# Agent CPU and wall-clock budgets

This document describes how `AgentContainerManager` (in
`services/runner/runner/agent_container_manager.py`) bounds an agent's
resource usage during a match. There are exactly five enforcement rules:

| Rule                          | Where it fires | Bounds                                              | Catches                                                              |
|-------------------------------|----------------|-----------------------------------------------------|----------------------------------------------------------------------|
| Per-step CPU cap              | poll loop      | CPU time used in one step (busy_ratio gated)        | Heavy single-step computation; infinite-loop attacks                 |
| Per-step wall guard           | poll loop      | Wall time on one step *while CPU ≈ 0*               | Sleepers, hangs, agents that block on I/O                            |
| Sustained CPU bank            | notify_step    | Long-run average CPU per step (busy_ratio gated)    | Agents under the per-step cap but above the long-run rate            |
| Sustained wall bank           | notify_step    | Long-run "excess wall" per step                     | Sleep-just-under-the-per-step-line over many steps                   |
| Snake-died check              | notify_step    | —                                                   | In-game death; not a budget violation, just cleans up the container  |

The configured values in `match.py`:

```
per_step_cpu_budget_seconds       = 0.05    # 50 ms CPU per step
per_step_wall_budget_seconds      = 1.0     # 1 s wall (only fires when CPU ≈ 0)
startup_cpu_budget_seconds        = 0.20    # 200 ms CPU during gRPC init
sustained_cpu_refill_seconds      = 0.01    # +10 ms CPU bank per busy step
sustained_cpu_initial_seconds     = 0.05    # bank starts at 50 ms
sustained_cpu_max_seconds         = 0.50    # bank caps at 500 ms
sustained_wall_refill_seconds     = 0.025   # +25 ms wall bank per step
sustained_wall_initial_seconds    = 0.50    # bank starts at 500 ms
sustained_wall_max_seconds        = 1.00    # bank caps at 1 s
```

Plus two module-level constants:

```
_WALL_K = 3.0          # wall_excess = max(0, wall - cpu * k)
_BUSY_RATIO = 0.5      # cpu/wall threshold for "agent was busy"
```

## How the budgets read CPU and wall time

CPU time comes from cgroup v2 `cpu.stat` (`usage_usec`) read directly. The
counter is cumulative; each measurement is `current - previous_snapshot`.

Two wall sources, used for different things:

- **Sim cycle wall** — `LoopStepData.total_time` (the wall the sim spent
  on one step). Used at `notify_step` for the sustained banks. Sourced
  from the sim because the manager's receive-time delta is inflated by
  TCP/queue lag between sim emission and manager dispatch.
- **Sim per-decision wall** — `LoopDecisionData.wall_time_ns` (the sim
  executor's time from handing the request to the future completing for
  this specific snake). Used as `response_wall` in the sustained-wall
  debit. Per-snake, lag-immune.
- **Manager monotonic** (`time.monotonic_ns()`) — used only by the poll
  loop's `cpu_in_step` / `wall_in_step` (since the poll loop fires
  mid-cycle and notify_step hasn't reported yet).

## The poll loop

Runs at ~10 ms cadence. Two checks per tick per tracker, plus the gates:

```python
for seat, tracker in trackers:
    if tracker.killed or tracker.in_startup or tracker.responded_this_step:
        continue
    current = read_cpu_ns(...)
    cpu_in_step = current - tracker.step_start_cpu_ns
    wall_in_step = monotonic_ns() - tracker.step_start_wall_ns
    busy = cpu_in_step / wall_in_step

    # Rule 1: per-step CPU cap.
    if cpu_in_step > per_step_cpu_budget and busy >= _BUSY_RATIO:
        kill("per_step"); continue

    # Rule 2: stalled — long wall, agent not actually using CPU.
    if wall_in_step > per_step_wall_budget and busy < _BUSY_RATIO:
        kill("wall_clock")
```

That's it. No PSI parsing, no contention history, no projected bank
kills, no startup-phase CPU monitoring, no adaptive wall budget.

### Why busy_ratio matters on the per-step CPU cap

A real match (match-f90295dd) showed why: an agent used 77 ms of cgroup
CPU during a cycle whose wall was 199 ms because the runner was
SIGKILLing two other dead containers. The agent's actual decision took
~5 ms; the rest was idle background work the container did while the
cycle was long. busy_ratio = 77/199 = 0.39, below 0.5 — the kill is
skipped. A real abuser burns CPU continuously: busy_ratio ≈ 1.0 (or at
least above 0.5) — the kill fires.

### Why we don't need adaptive wall

The per-step wall budget is 1 s and only triggers when the agent's
`busy_ratio` over that window is below 0.5 — i.e. the container was
mostly idle. Legitimate slow agents on a contended host burn CPU
proportionally to how slow they're being (busy_ratio stays high), so
they don't trip the guard. A sleeper (or one that's blocked on I/O)
has busy_ratio near 0 regardless of how big the container's background
ticks add up to, because the wall denominator grows along with the
window. That ratio-based check is what lets a fixed 1 s threshold work
without false-killing real work and without missing 30-second sleepers.

## Sustained CPU budget

Per `notify_step`:

```python
cpu_delta_for_bank = min(cpu_delta, sim_wall_step)   # see "the cap" below
busy = cpu_delta_for_bank / sim_wall_step
if busy >= _BUSY_RATIO:
    bank += sustained_cpu_refill - cpu_delta_for_bank
    bank  = min(bank, sustained_cpu_max)
    if bank < 0: kill("sustained")
```

The bank starts at `sustained_cpu_initial_seconds` (= per-step budget by
default — one fully-slow step is OK). `cpu_delta` is the *total* cgroup
CPU over the cycle, not just the decision-window CPU; users submit
custom images and can spawn threads, so the budget has to see all CPU
the container burns. `nano_cpus = 1.0` is the absolute hardware ceiling;
this bank is the softer per-decision rate below it.

### The cap on `cpu_delta`

`cpu_delta` is the cgroup CPU consumed between two consecutive
manager-side `notify_step` processings. Under normal flow that window
matches `sim_wall_step` closely (the manager processes each frame
~immediately after the sim emits it). But the poll loop and the
observer reader share the manager-side lock, so if the poll loop is
mid-cycle doing cgroup reads when notify_step frames arrive, several
frames queue up behind it. The **first** notify_step processed after
the lock-hold sees a `cpu_delta` that spans the entire lock-hold window
— multiple sim steps' worth of CPU — but `data.total_time` only reports
the current step's wall. Without a cap, that first event debits prior
steps' CPU against this step's refill and false-kills the agent: a real
match produced `busy = 46.5` because `cpu_delta = 34 ms` got divided by
a `sim_wall_step` of 0.73 ms.

Capping `cpu_delta` at `sim_wall_step` works because `nano_cpus = 1.0`
makes that the physical ceiling anyway — the agent literally could not
have used more CPU than wall during the current sim step. The excess
(`cpu_delta - sim_wall_step`) belongs to prior sim steps the manager
processed out-of-band; that accounting is "leaked" from the bank, but:

1. `last_cpu_ns` still advances to `current`, so the leak doesn't
   cascade across cycles.
2. Attackers can't trigger manager-side lock contention from inside
   their container.
3. `nano_cpus = 1.0` is the absolute backstop for total CPU.

`exec_times.json` still records the raw, uncapped `cpu_delta` for
diagnostics, so post-mortems can see when a catch-up burst happened.

### The busy_ratio gate on the bank

Same reason as the per-step CPU cap: a wide cgroup window with mostly
background CPU shouldn't be charged against the 10 ms/step rate. When
`busy_ratio < 0.5` we skip refill *and* debit — the agent neither earns
credit nor pays for an idle window. If the agent is actually sleeping
(low CPU, high wall), the *wall* bank catches it on the same call.

### What if an agent oscillates around the threshold?

An agent that hovers between busy_ratio 0.4 and 0.6 will see some
cycles debit and some skip. Over time the busy cycles drain the bank if
the agent is consuming more than `sustained_cpu_refill` per cycle on
average; the idle cycles are correctly free. An attacker can't profit:
the only "free" cycles are ones where they weren't really using CPU
anyway, capped to whatever fraction of the time they fall below 0.5.

## Sustained wall budget

Bounds long-run "sleep just under the line" abuse. Per `notify_step`:

```python
response_wall = decision_data.wall_time_ns or sim_wall_step  # sim-measured
excess        = max(0, response_wall - cpu_delta * _WALL_K)
bank         += sustained_wall_refill - excess
bank          = min(bank, sustained_wall_max)
if bank < 0: kill("sustained_wall")
```

`response_wall` is the sim's per-snake decision wall. Per-snake (not the
global cycle) so a fast snake stuck behind a slow neighbour pays nothing
— its own `response_wall` is tiny.

`_WALL_K = 3.0` is a fixed wall-to-CPU ratio. An agent that used 50 ms
CPU is "expected" to take up to 150 ms wall; only the part above that
counts as excess. No adaptive contention tier — on dedicated runner
hosts the real contention is close to 1× anyway, and 3× absorbs normal
sim overhead, gRPC dispatch, and scheduling jitter. If host pressure
becomes a real production issue, add adaptive contention back with data
showing the false-positive rate.

### Sleeper example with defaults

Sleeping 1 s/step with ~0 ms CPU:
- response_wall = 1000 ms, cpu × k = 0 ms → excess = 1000 ms
- bank: +25 ms refill − 1000 ms excess = −975 ms / step
- Bank starts at 500 ms → goes negative on the *first* step. Killed.

Sleeping 80 ms/step (just under a hypothetical tighter per-step wall):
- response_wall = 80 ms, cpu × k = 0 ms → excess = 80 ms
- bank: +25 ms − 80 ms = −55 ms / step
- Bank lasts ~9 steps before dropping below zero. Total stolen wall ≈ 720 ms.

Legitimate fast agent (50 ms CPU, 75 ms wall under mild contention):
- response_wall = 75 ms, cpu × k = 150 ms → excess = 0 ms
- bank: +25 ms − 0 = +25 ms / step, fills to cap and sits there.

## Startup phase

Between `set_agent_containers` and `notify_start`, the agent's container
is running its constructor and the sim's gRPC init handshake. The
budget is `startup_cpu_budget_seconds`. Enforcement is **one-shot at
notify_start**: when the sim publishes its `snake_tags`, the manager
reads each container's current cgroup CPU and compares to the baseline
captured at `set_agent_containers`. Over-budget seats are killed with
reason `startup_cpu`.

No continuous startup monitoring during the init handshake itself —
the gRPC channel-ready timeout (typically 10 s, set in `match.py`) is
the bound on how long this phase can take, and `nano_cpus = 1.0` is
the absolute CPU cap during it.

## Kill reasons and dev-console banners

`_AgentTracker.kill_reason` is set at every kill site to one of:

| Reason             | Meaning                                                                                   |
|--------------------|-------------------------------------------------------------------------------------------|
| `startup_cpu`      | Startup CPU one-shot check at notify_start exceeded budget                                |
| `per_step`         | Poll-loop per-step CPU cap (with busy_ratio guard) tripped                                |
| `sustained`        | Sustained CPU bank went negative                                                          |
| `wall_clock`       | Poll-loop per-step wall guard tripped (long wall, ~no CPU — sleeper)                      |
| `sustained_wall`   | Sustained wall bank went negative                                                         |
| `init_failure`     | Sim dropped the seat during gRPC init                                                     |
| `dead`             | `alive_states[snake] = False` — snake died in-game                                        |

`runner/match.py:_budget_kill_note` translates each reason (except
`dead`) into a human-readable banner prepended to the dev agent's
final step log.

Match-level termination (end the match once the dev dies, or once one
snake remains alive AND is the longest) is enforced by the sim itself
via the `--end-when-dead-tag` / `--end-on-last-standing-when-longest`
flags — not by this manager. When those rules fire, opponent
containers stay alive until `_cleanup` tears the match down, so they
end the match with `kill_reason = None`.

## What this design deliberately does NOT have

The current implementation is a rewrite of a previous design that
included several signals that turned out not to pay for their
complexity. The omissions are intentional:

- **PSI-based contention factor.** Previously the wall budget adapted
  via `/proc/pressure/cpu` and a rolling history of `wall/cpu` per
  step. Dropped — on dedicated runner hosts the contention factor was
  near 1.0 nearly always, and the adaptive tier had its own failure
  modes (a sleeping agent's own slow wall divided by its tiny CPU
  produced an enormous "contention" reading that the same agent then
  used to justify its own slow wall).
- **Projected mid-step bank kills.** The poll loop used to estimate
  "the bank will go negative when this step closes" and kill mid-cycle.
  Two-checks-only is enough: the per-step CPU cap (rule 1) catches the
  same scenario for active CPU abuse, and the sustained bank handles
  it at the next `notify_step` for slower-developing cases.
- **First-step extra budget (`initial_budget_seconds`).** Previously
  the first cycle got `4 × per_step_budget` of CPU credit to absorb
  JIT warmup. Dropped — agents should warm up during init (where they
  have 200 ms of startup CPU), not on the first real decision.
- **Stretched-cycle skip flag.** Briefly attempted — a global "if
  sim_wall > 3× budget, skip both banks for this cycle." Replaced by
  the per-seat busy_ratio gate on the CPU bank, which is the same idea
  without the sleeper hole (a global cycle skip would also skip the
  agent *causing* the stretch).

## Failure modes worth understanding

- **Cgroup file vanishes mid-poll**: `_read_cpu_ns` returns -1, the
  tracker is skipped that iteration. The cycle's `cpu_delta` falls
  back to 0 at the next `notify_step` and `alive_states` flips the
  seat to `dead`. No double-kills.
- **First step is JIT-heavy**: the agent's first decision may exceed
  the per-step CPU budget if it pays for first-import or first-call
  costs that weren't already paid in the constructor. The fix is to
  warm up in the constructor, where the startup CPU budget is 200 ms
  (4× the per-step budget) and the gRPC channel-ready timeout is the
  bound. If first-decision JIT becomes a real problem in production,
  reintroduce a small `initial_budget_seconds` knob.
- **All-sleepers match**: per-step wall guard kills each as their
  cycle wall passes 1 s. Match ends quickly.
- **Manager re-used across matches**: `notify_start` re-initialises
  banks and clears the per-seat data dicts. `set_agent_containers`
  rebuilds the trackers. No cross-match state.
- **A normal cycle's wall is shorter than `step_start_wall_ns`'s
  resolution**: `wall_in_step = 0` and busy_ratio falls back to 0.
  Neither poll-loop rule fires (cpu_in_step won't have exceeded the
  per-step cap in <1 ms of wall, and wall_in_step won't be over 1 s).
  Safe — the next iteration has measurable wall.
