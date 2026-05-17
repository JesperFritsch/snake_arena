# snake_arena — Project State Snapshot

**Owner:** Jesper Fritsch (solo developer, based in Sweden)
**Type:** Hobby / portfolio project
**Target hosting:** Hetzner CCX13 VM (eventually). Currently developing locally + in a libvirt Ubuntu VM for sandbox testing.
**Budget cap:** 200 SEK/month total operating cost.
**Last snapshot:** 2026-05-15 (derived from the source conversation up to this point).

## What snake_arena is

A competitive snake AI tournament platform. Users submit snake-playing agents as code; the platform builds them into sandboxed Docker images, runs matches between them in a deterministic simulator, and produces replays + analysis + leaderboards.

Two execution modes:

- **Test mode** — user iterates, runs their agent against built-in bots or each other. Latency-sensitive.
- **Tournament mode** — scheduled bulk runs across all current submissions; latency-insensitive.

The simulator is the existing `snake_sim` project (separate repo). `snake_arena` builds the arena infrastructure around it: builder, runner, sandbox base images, storage, web layer (eventually).

## Repos / packages

- **`snake_sim`** — separate repo. The deterministic simulator, gRPC contract for external agents, observer infrastructure. Reused as a dependency in `snake_arena`.
- **`snake_arena`** — this project. uv workspace with these member packages:
  - `services/sa_common/` (was previously `common`, renamed to `sa_common`) — shared types, db layer, etc.
  - `services/runner/` — orchestrates a single match.
  - `services/builder/` — turns submitted code into a tagged Docker image.
  - `sandbox-images/python/` — Python base sandbox image (harness, gRPC stubs).
  - `sim-image/` — containerized snake_sim.
  - `proto/` — shared gRPC contracts.

## What is working end-to-end

- Python sandbox base image with harness + generated gRPC stubs + `manifest.toml` for language metadata. Hardened: non-root user, no caps, `--cpus=1.0`.
- Sim image (containerized `snake_sim`) talks gRPC to agents and writes replays to a mounted artifacts volume.
- Builder service (Python function + CLI): takes user code + language → produces a tagged submission image. Discovers supported languages from `manifest.toml`.
- Runner service (Python function + CLI): takes a sim image + N submission images, creates one isolated network per agent + one shared network for the sim's runner callback, runs the match, captures replays + logs, cleans up.
- gVisor (`runsc`) installed and validated in the Ubuntu VM — kernel info differs inside sandbox, sibling agents unreachable after per-agent networks.
- Hostile agent suite (`hostile/`): fork bomb, mem bomb, disk fill, network probe, privesc, cpu spin, kernel-info probe. All confirmed contained by the sandbox.
- Bidirectional socket observer + observable between sim and runner. Sim publishes step data; runner can issue `KillAgent` commands.
- `CpuBudgetObserver` in the runner: reads each agent container's CPU usage directly from cgroups every 10 ms in a per-container thread, enforces a cumulative per-step CPU budget, sends a kill command to the sim and stops the container when an agent exceeds it.
- Snake naming: external snake target hostname (e.g. `agent1:50051`) used as the tag in the sim. Sim's `LoopStepData.snake_tags` round-trips this to consumers.
- Postgres in Docker (locally for now): `matches` + `match_participants` tables. Runner persists a row per match after computing the result. Best-effort write — DB failure does not fail the match.
- Per-snake response timing (`snake_times`) is in every step's data and reaches the replay, but per-agent timing **summary stats** are not yet computed/persisted.

## What is not yet built (rough priority)

1. **A second language base image** (Rust or Go). Proves the language-agnostic design is real, not theoretical.
2. **Submissions table + FK from `match_participants`**. Right now participants don't link back to a specific code version / submission.
3. **Users / projects / code_versions tables**, in that order. Each is its own migration.
4. **Per-agent timing summary** (mean / p50 / p95 / p99 / max response time, total CPU, std dev) computed in `run_analyzer` and surfaced to `MatchResult` so the per-participant row can store it.
5. **Job queue.** Replace runner CLI with polling a `jobs` table in Postgres. Decouples "request a match" from "run a match" and is the substrate for tournaments.
6. **Web API** (FastAPI) + frontend (Monaco-based editor) — the user-facing surface.
7. **Tournament scheduler** — bulk match runs across current submissions, leaderboard.
8. **Cloudflare R2 wiring** — currently replays land on local disk; the storage design says they should flow to R2 from day one once the platform leaves the dev box.

## Known smaller items flagged but deferred

- gVisor only installed in the Ubuntu test VM, not on the Arch dev box. Local dev is fine without it; production gets it.
- Sim writes the replay file on shutdown via a worker thread, joined at sim shutdown. Runner historically raced this; current behavior is OK because the join waits, but a poll-with-timeout in the runner would be more defensive.
- `killed_by_budget` column is currently hardcoded `False` when writing match_participants. Needs to be plumbed through from `CpuBudgetObserver`.
- `submission_id` column on `match_participants` is intentionally absent until a `submissions` table exists.
- No connection pooling on Postgres yet — runner opens one connection per match write. Fine until the API layer needs concurrency.
- No retention / GC policies defined for replays or container images yet.

## Top-level architecture in one sentence

One VM running everything (Postgres + builder + runner + sim/agent containers + reverse proxy), Cloudflare R2 for replays + analysis + DB backups, Cloudflare in front for TLS / DDoS, gVisor + container hardening + per-agent networks for sandboxing, per-step CPU-time budget enforced by the runner reading cgroups directly.

## Pointers to the other RAG files

- Architecture and decisions with rationale → `02_architecture_and_decisions.md`
- Subsystem details (runner, builder, sim, observer protocol, CPU budget) → `03_subsystems.md`
- Storage design (the canonical document) → `04_storage_design.md`
- Database schema, migrations, and data-layer conventions → `05_database_and_schema.md`
- Custom instructions / working style → `06_custom_instructions.md`
