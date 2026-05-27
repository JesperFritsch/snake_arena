# Agent CPU and wall-clock budgets

This document describes how `AgentContainerManager` (in
`services/runner/runner/agent_container_manager.py`) bounds an agent's
resource usage during a match. There are four independent budget families:

| Budget                      | Bounds                                          | Detects                                                           |
|-----------------------------|-------------------------------------------------|-------------------------------------------------------------------|
| Per-step CPU                | CPU time used in a single step                  | Heavy single-step computation; infinite-loop attacks              |
| Sustained CPU               | Long-run average CPU per step                   | Agents under the per-step cap but above the long-run rate         |
| Per-step wall-clock         | Wall time spent on a single step (adaptive)     | Agents that sleep / block instead of computing                    |
| Sustained wall-clock        | Long-run non-CPU wall time per step             | Agents sleeping *just under* the per-step wall threshold          |

The per-step budgets catch single-step abuse; the sustained budgets are
backstops that catch the "just under the line every step" attack patterns
the per-step budgets miss.

The configured values in `match.py` are:

```
per_step_budget_seconds            = 0.05     # 50 ms CPU per step
initial_budget_seconds             = 0.20     # +200 ms one-shot on first step
startup_budget_seconds             = 0.20     # 200 ms CPU during gRPC init

accumulating_step_seconds          = 0.01     # +10 ms CPU bank per step
accumulating_initial_seconds       = 0.05     # bank starts at 50 ms
accumulating_max_seconds           = 0.50     # bank caps at 500 ms

wall_clock_safety_factor           = 3.0      # safety×3 on top of measured contention
wall_clock_hard_floor_seconds      = 1.0      # never under 1 s
wall_clock_sleep_threshold_seconds = 0.001    # < 1 ms CPU in window = sleeping
wall_clock_psi_path                = /proc/pressure/cpu
wall_clock_psi_scale_k             = 1.0
wall_clock_fallback_contention     = 5.0
wall_clock_min_history             = 3
wall_clock_history_size            = 8
wall_clock_max_contention          = 5.0    # cap on contention factor
wall_clock_contention_min_cpu_seconds = 0.005  # 5ms — sample inclusion floor

wall_accumulating_step_seconds     = 0.05     # +50 ms wall bank per step
wall_accumulating_initial_seconds  = 0.50     # bank starts at 500 ms
wall_accumulating_max_seconds      = 1.00     # bank caps at 1 s
```

## How the budgets read CPU time

CPU time is read from cgroup v2 `cpu.stat` (`usage_usec`) directly, not from
the Docker stats API. Reading is O(1), works without elevated privileges, and
gives true on-CPU time — independent of how loaded the runner is. Wall time
comes from `time.monotonic_ns()` in the runner process.

## Per-step CPU budget

Each step the agent gets `per_step_budget_seconds` of CPU time. The very
first step also gets `initial_budget_seconds` extra, to absorb JIT warm-up
and first-call cache-miss overhead. The poll loop (~10 ms cadence) reads
each agent's cgroup `cpu.stat`; if `cpu_used_in_step > step_budget`, the
agent is killed.

During the startup phase (`set_agent_containers` → `notify_start`) the
budget is `startup_budget_seconds`. The same poll loop enforces it, but the
kill reason is labeled separately so the user-facing banner can say "killed
during startup."

## Sustained CPU budget

Each agent keeps a CPU bank. Each `notify_step`:

```
bank += accumulating_step_seconds        # refill
bank -= cpu_used_this_step               # debit
bank  = min(bank, accumulating_max_seconds)
if bank < 0: kill
```

The bank starts at `accumulating_initial_seconds` (= per_step_budget by
default — one fully-slow step is OK).

With defaults, an agent averaging > 10 ms CPU per step eventually drains
the bank. A bursty agent that uses 50 ms one step and 0 ms the next is
fine. The cap prevents an agent from "saving up" indefinitely.

## Per-step wall-clock budget (contention-adaptive)

A single fixed wall-clock threshold is the wrong tool here: a 1.5 s
threshold is too loose on an idle host (a sleeper attack can do
1.4 s/step) and too tight on a loaded host (a legitimate slow agent gets
false-killed).

So the budget is computed adaptively each iteration of the poll loop as:

```
budget = max(hard_floor, per_step_cpu_budget * contention * safety)
```

The `contention` factor is the multiplier applied to CPU time to estimate
expected wall time. It's computed in three tiers — the first tier that has
data wins:

1. **Measured**: rolling mean of `wall_step / max_cpu_per_step` from the
   last K completed steps (K = `wall_clock_history_size`, default 8),
   provided there are at least `wall_clock_min_history` (3) samples whose
   `max_cpu_per_step ≥ wall_clock_contention_min_cpu_seconds` (default
   5 ms). This is the most accurate signal — it reflects whatever
   contention, scheduler quirks, sim overhead, etc. are actually slowing
   this match down right now. The CPU floor on samples matters: without
   it, a sleeping agent's slow wall divided by its tiny cpu produces an
   enormous "contention" reading that the same agent then uses to justify
   its own slow wall, defeating the sustained-wall budget. Steps where
   every agent was sleeping / trivial are skipped instead.
2. **PSI**: read `/proc/pressure/cpu` (or a configured per-cgroup path),
   parse the `some avg10` value (% of time something was waiting for CPU
   in the last 10s), translate to a multiplier: `1 + avg10/100 * k`. Used
   when we don't yet have enough completed steps for tier 1 (cold start,
   or every step has been stalled by a sleeper so nothing completes).
3. **Fallback constant**: `wall_clock_fallback_contention` (default 5).
   Used when both above are unavailable — e.g. running on a kernel without
   PSI, or with `wall_clock_psi_path = None`.

All three tiers are clamped to `[1.0, wall_clock_max_contention]`. The
lower bound stops the budget from claiming "wall is faster than CPU" (which
would shrink it below the CPU budget). The upper cap (default 5×) is a
safety net: real host contention almost never exceeds 5×, and anything
much higher than that almost certainly indicates a measurement polluted by
a misbehaving agent. Without the cap, the sustained-wall budget below
would self-defeat against a slow-but-not-quite-killable sleeper.

The kill rule is two-part: kill if `wall_elapsed > budget` **AND**
`cpu_used_in_window ≤ wall_clock_sleep_threshold_seconds`. The CPU
conjunction is what distinguishes a sleeping agent (0 CPU) from a
legitimate agent burning its full CPU budget under load. Setting the
threshold low (1 ms) means a trivial real agent — which still has to
decode protobuf, dispatch into user code, and encode a response — always
has > 1 ms CPU and is never killed; only a `time.sleep`-style stall
trips the check.

## Sustained wall-clock budget

The per-step wall budget kills sleepers fast, but it has a knee: an
attacker who sleeps for *just under* the budget every step blows up total
match time without ever tripping any single-step kill. With a 1.5 s
per-step budget and 1.4 s of sleeping per step, a 1000-step match becomes
23 minutes of nothing.

The sustained wall budget bounds this. Each agent has a wall bank. Each
`notify_step`:

```
expected_wall_for_agent = cpu_used_this_step * contention
excess_wall             = max(0, wall_step - expected_wall_for_agent)
bank                   += wall_accumulating_step_seconds  # refill
bank                   -= excess_wall                     # debit
bank                    = min(bank, wall_accumulating_max_seconds)
if bank < 0: kill
```

Why `cpu * contention` is the right "expected" floor: it's the wall time
the agent's CPU work should have consumed under current contention. An
agent that used 50 ms CPU under 3× contention legitimately should have
taken ~150 ms of wall. If `wall_step` was 150 ms, `excess = 0`. If
`wall_step` was 1500 ms because the agent slept 1350 ms after computing
50 ms, `excess = 1350 ms` and the bank drains.

With defaults (refill 50 ms/step, max 1 s, initial 500 ms):

- Attacker doing `sleep(1.4 s)`/step: `excess ≈ 1400 ms` per step, refill
  50 ms, net `-1350 ms`. Bank empties in roughly one step.
- Attacker doing `sleep(80 ms)`/step (just under a hypothetical tight
  budget): `excess ≈ 80 ms`, refill 50 ms, net `-30 ms`. Bank lasts ~17
  steps. Match capped at ~1.4 s of stolen wall total.
- Legitimate agent on a contended runner: `excess ≈ 0`, bank fills to the
  cap and sits there.

### A note on collateral damage

If a sleeper is in a match, every other agent's `wall_step` is bloated by
the sleeper's blocking (because `notify_step` only fires when all agents
respond). The fast/innocent agents have `cpu * contention` ≪ `wall_step`,
so their wall banks drain too — they look the same as the sleeper from
this signal alone.

This is acceptable because the **per-step** wall budget kills sleepers
within ~10 ms of crossing the threshold. The sustained-wall budget is a
backstop, not the primary detector. Per-step does the actual catching;
sustained-wall just makes sure repeated near-misses can't accumulate into
a denial-of-service.

## Where the sustained kill happens vs the per-step kill

| Where         | Trigger                            | When                                                                                           |
|---------------|------------------------------------|------------------------------------------------------------------------------------------------|
| Poll loop     | per-step CPU over budget           | Within ~10 ms of crossing the per-step CPU budget                                              |
| Poll loop     | per-step wall over budget AND ~0 CPU | Within ~10 ms of crossing the adaptive per-step wall budget                                  |
| notify_step   | sustained CPU bank < 0             | At the step boundary that made it cross                                                        |
| notify_step   | sustained wall bank < 0            | At the step boundary that made it cross                                                        |
| notify_start  | seat absent from match start       | When the sim dropped the seat during gRPC init                                                |
| notify_step   | alive_states[snake] = False        | When the sim marks the snake dead in-game                                                      |

## Kill reasons and dev-console banners

`AgentContainerManager._AgentTracker.kill_reason` is set at every kill site
to one of:

| Reason             | Meaning                                                                                   |
|--------------------|-------------------------------------------------------------------------------------------|
| `startup_cpu`      | Per-step CPU budget tripped while still in startup phase                                  |
| `per_step`         | Per-step CPU budget tripped in normal play                                                |
| `sustained`        | Sustained CPU bank went negative                                                          |
| `wall_clock`       | Per-step adaptive wall budget tripped, CPU was ~0 in window (sleeper)                     |
| `sustained_wall`   | Sustained wall bank went negative (chronic sleep-just-under-the-line)                     |
| `init_failure`     | Sim dropped the seat during gRPC init (wall-clock timeout, agent crash before connecting) |
| `dead`             | `alive_states[snake] = False` — snake died in-game, normal outcome                        |

`runner/match.py:_budget_kill_note` translates each reason (except `dead`)
into a human-readable banner that's prepended to the dev agent's final
step log in the test-match console. Example for `sustained_wall`:

```
=== Agent killed: exceeded the sustained wall-clock budget (non-CPU wall
time grew faster than 50 ms/step; up to 1000 ms of saved credit allowed).
The agent kept sleeping just under the per-step wall-clock limit. ===
```

## Bundle metadata

Every match bundle includes a `budgets.json` describing the manager's
configured knobs (per `AgentContainerManager.get_budgets()`). For
adaptive budgets, the recorded values are the *knobs that drive the
computation* — `wall_clock_safety_factor`, `psi_scale_k`, etc. — not the
actual per-step budget at each tick (that varies). Combined with
`exec_times.json`, a bundle reader can reconstruct what would have been
considered "over budget" at any moment.

## Tuning advice

- **`wall_clock_safety_factor`** is the "I trust agents to be a bit
  variable" knob. Pure CPU contention is captured by the measured
  contention factor; this multiplier is the slack on top. Bump it up if
  legitimate agents are getting false-killed; bump it down if sleepers
  are surviving too long.
- **`wall_accumulating_step_seconds`** is the average sleep budget per
  step. Tighter (lower) = strictly bounded total damage from a sleeper,
  at the cost of false-killing agents during sustained host pressure
  spikes. Looser (higher) = more tolerant of host variance, but a slow
  attacker can do more steps.
- **`wall_clock_hard_floor_seconds`** prevents an idle host (contention
  ~ 1) from producing absurdly tight budgets. Don't drop below the
  worst-case sum of all agents' CPU plus sim overhead on the busiest
  conceivable step.
- **`wall_clock_fallback_contention`** matters only when neither measured
  contention nor PSI is available — e.g., the first three steps of the
  very first match after a runner restart, on a kernel without PSI. Pick
  a value that's safely *generous* — false-negative (sleeper survives an
  extra second) is much cheaper than false-positive (kill a real agent
  during cold start).

## Failure modes worth understanding

- **Kernel without PSI / no `cpu.pressure`**: tier 2 silently returns
  `None` and we fall through to the fallback constant. No crash, no
  degraded enforcement after the third completed step (tier 1 takes over).
- **All-sleepers match**: tier 1 never gets samples (no step completes).
  Tier 2 reflects whatever the host is doing. Tier 3 is the floor. The
  per-step wall budget still kills each sleeper as it crosses the
  threshold; the match ends quickly.
- **Container restart mid-poll-cycle**: `_read_cpu_ns` returns `-1` if
  cgroup files vanish; the tracker is skipped that iteration. No double
  kills.
- **Manager re-used across matches**: `notify_start` resets per-tracker
  state and clears `_step_history`. The first few steps of each new match
  start in tier 2/3 territory until tier 1 has data again.
