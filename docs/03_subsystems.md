# Subsystems

Detailed notes on each subsystem, written for someone (or some future Claude) walking into the code cold.

## Match runner

Located at `services/runner/`. Currently a Python function + CLI; future versions will poll a jobs table.

### Responsibilities of `run_match`

1. Pull / verify the N submission images and the sim image exist locally.
2. Create one Docker network per agent (`internal: true`) + one network for the sim's runner callback (not internal).
3. Start each agent container on its own per-agent network, with sandbox flags and resource limits (`--cpus=1.0`, non-root, `runtime=runsc` in the VM, no caps).
4. Wait for each agent's gRPC port to be ready (with timeout).
5. Start the sim container, attached to every per-agent network (so it can reach each agent) **and** to the runner-callback network. Pass `--external-snake-targets agent1:50051,agent2:50051,...` and the address of the runner's socket listener.
6. Spawn the supervisor threads:
   - The socket observable's accept thread (accepts the sim's connection, reads step data).
   - The `CpuBudgetObserver` thread per agent (10 ms cgroup poll).
7. Wait for the sim container to exit (with hard match-level wall-clock timeout as backstop).
8. Read the replay file from the mounted artifacts volume.
9. Tear down: stop all containers, remove all networks. Always run cleanup, even on exceptions.
10. Return a `MatchResult` (winner, scores, replay path, per-agent stats, crash flags, sim exit code).
11. After computing the result, best-effort persist to Postgres via `sa_common.db.record_match_result`. Wrapped in try/except — DB failure must not fail the match.

### Failure modes are expected outcomes, not exceptions

The runner returns a `MatchResult` for any input. It never propagates a crash from an agent, a misbehaving image, or a sim error to the caller. Expected outcomes:

- Agent fails to start (image broken, gRPC never opens).
- Agent crashes mid-match.
- Agent hangs / exceeds CPU budget.
- Sim crashes.
- Out-of-memory.
- Match exceeds wall-clock limit.
- Sandbox escape attempt (logged, treated as crash).

### Artifacts directory

A host directory is bind-mounted into the sim and agents at `/tmp` (so the sim can write replays and agents can write logs). The runner sets the permissions on the host dir before the mount so the in-container uid can write to it.

Layout under the artifacts host dir per match:
```
artifacts/
├── runs/                      # replay protos (.run_proto)
├── <match-id>-agent1.log
├── <match-id>-agent2.log
└── <match-id>-sim.log
```

### Cleanup guarantees

The runner script has been hardened so that containers always get removed, even on Ctrl-C or exceptions during cleanup:

- All containers tracked in a list at creation time.
- Cleanup uses try/except per container, calling `container.stop(timeout=2)` then `container.remove(force=True)`. Continues through failures.
- Same for networks.
- A daemon supervisor thread is not relied on for cleanup; the main thread's `finally` block does it.

## Builder service

Located at `services/builder/`. Python function + CLI.

### Flow

1. Input: language string, code blob (file or string), agent name.
2. Look up the language's base image and Dockerfile template from `manifest.toml`.
3. Write the user's code to a temp build context.
4. Build the final image with `docker build` (BuildKit), tagging it.
5. Output: tag string, success/failure.

### `manifest.toml` per language

Each base sandbox image directory (e.g. `sandbox-images/python/`) ships a `manifest.toml` declaring:

- Language name + version.
- Base image tag.
- How the user's code gets copied in (target path, file naming convention).
- The harness entrypoint that runs the gRPC server.

The builder discovers languages dynamically from the available `manifest.toml` files — **no hardcoded list of supported languages**. Adding a new language is: drop a new `sandbox-images/<lang>/` dir with a `Dockerfile` and a `manifest.toml`, and the builder picks it up.

### Submission image structure (Python example)

The base image already contains:
- Generated gRPC stubs from `proto/simrun.proto`.
- The harness (`harness.py`) that imports the user's snake class, instantiates it, and serves the gRPC contract.
- A non-root user.
- All language deps locked.

The final per-submission image only adds:
- The user's code at a known path.
- The user's optional `requirements.txt` (Python).

This makes builds fast and keeps the security surface small.

## Sim (snake_sim, used by snake_arena)

`snake_sim` is its own repo. `snake_arena` consumes it as a dependency. The sim:

- Loads a map, spawns N snakes.
- Each step, calls `Update(state) → decision` on every external snake (over gRPC) and every in-process snake.
- The loop runs through `ILoopObservable`; loop observers (`ILoopObserver`) see `LoopStartData`, one `LoopStepData` per step, and `LoopStopData` at the end.
- Writes the replay file (`.run_proto`) at shutdown via a worker thread.

### Snake naming inside the sim

External snake target hostname (as passed in `--external-snake-targets`, e.g. `agent1:50051`) is used as the snake's tag in the sim. The sim's metadata (`EnvMetaData.snake_tags: dict[snake_id, str]`) makes this available to all observers and to the replay/analysis pipeline. No separate naming layer was needed.

### Observer protocol over the socket

The sim has a `SocketObserver` (the observer side; outbound step data) and a corresponding observable in the runner. The runner is the TCP server; the sim connects out.

Message types currently defined:

- `LoopStartMsg` — once, at the start. Carries `EnvMetaData` (map, snake_tags, etc.).
- `LoopStepMsg` — once per step. Carries `LoopStepData`.
- `LoopStopMsg` — once, at the end. Carries `LoopStopData`.
- `KillAgent` — runner → sim. Carries `{agent_id, reason}`. After receiving this, the sim marks the corresponding snake as dead and stops calling `Update` on it.

Framing: 5-byte header `>BI` (message type byte + uint32 length) + protobuf payload. Read with `_read_exact`.

The socket observer can use Unix sockets as well as TCP — the binding is whatever address is supplied. For local dev where sim and runner are on the same host, Unix sockets are faster; in production with containers, TCP through the bridge gateway is used.

### Why a single connection, not multiple observers

The runner is the one consumer at the moment. If a web UI or telemetry consumer is needed later, run a relay process that reads from the socket and fans out. Keeps the sim simple; doesn't bake fan-out into a place where it's rarely needed.

## CPU budget enforcement (`CpuBudgetObserver`)

Lives in the runner. Implements `ILoopObserver`. Why it's an observer in the runner: every `LoopStepMsg` is the trigger for "another step's worth of CPU budget granted."

### Algorithm

Maintain per-agent state:
- `last_cpu_ns` — last cgroup reading.
- `cumulative_budget_ns` — total CPU time granted across all steps so far.
- `killed` — set of agent_ids already terminated.

Each tick of the per-container thread (every ~10 ms):
- Read `cpuacct.usage` (cgroup v1) or `cpu.stat`'s `usage_usec` (cgroup v2) for the container.
- If the agent is no longer alive in the sim's view, skip.
- If `current_cpu_ns > cumulative_budget_ns`, the agent is over budget:
  - Mark killed.
  - Send `KillAgent(agent_id, reason="cpu_budget_exceeded")` over the socket to the sim.
  - `container.stop(timeout=2)`.

Each `LoopStepMsg` received from the sim grants the next slice of budget:
```python
cumulative_budget_ns[agent_id] += int(per_step_budget_seconds * 1e9)
```

### Why "cumulative budget" not "per-step deadline"

A per-step deadline kills agents that occasionally do a deep search even if their average is well below budget. Cumulative is fairer: an agent gets `T = per_step * step_count` CPU time so far; what they don't spend, they save.

### Why read cgroups directly instead of using the Docker API

`docker stats` is slow and adds latency. Reading `/sys/fs/cgroup/...` directly takes microseconds. The runner already knows the container's cgroup path from the container ID.

### Why a thread per container, not asyncio

The cgroup poll has to happen every ~10 ms reliably. With one thread per container, contention with the runner's main work and the socket reader is minimized; if one read is slow, it doesn't block the others. Asyncio would technically work but adds a layer of complexity for a non-IO-bound task that profits from real preemption.

### Wall-time backstop in the sim

The sim still has a wall-clock match timeout (~60 seconds, very loose). It is not the fairness mechanism; it exists only to prevent infinite hangs if the runner crashes or the socket dies. Real fairness lives in the runner.

## Job orchestration (planned, not built)

- Polling, not pub/sub. A `jobs` table in Postgres with `(id, kind, status, payload, created_at, started_at, finished_at, error)`.
- Runner becomes a worker loop: `LOOP { take a job; run it; mark done; sleep if empty }`. Single worker per host at v1.
- `kind` field decides what to do: `test_match`, `tournament_match`, etc.
- Test runs (user-triggered, latency-sensitive) and tournament runs (background) can share the table; tournaments get lower priority.

Redis-as-queue was considered and rejected: more infra, lower latency than is actually needed at this scale. Postgres polling is ~10 lines of code.
