# snake_arena — Project State Snapshot

**Owner:** Jesper Fritsch (solo developer, based in Sweden)
**Type:** Hobby / portfolio project
**Target hosting:** Hetzner CCX13 VM (eventually). Currently developing locally + in a libvirt Ubuntu VM for sandbox testing.
**Budget cap:** 200 SEK/month total operating cost.
**Last snapshot:** 2026-05-28

## What snake_arena is

A competitive snake AI tournament platform. Users submit snake-playing agents as code; the platform builds them into sandboxed Docker images, runs matches between them in a deterministic simulator, and produces replays + analysis + leaderboards.

Two execution modes:

- **Test mode** — user iterates, runs their agent against submitted opponents. Latency-sensitive; build is inline.
- **Tournament mode** — scheduled bulk runs across all current submissions; latency-insensitive.

The simulator is the existing `snake_sim` project (separate repo). `snake_arena` builds the arena infrastructure around it: builder, runner, orchestrator, API, frontend, sandbox base images, storage.

## Repos / packages

- **`snake_sim`** — separate repo. The deterministic simulator, gRPC contract for external agents, observer infrastructure. Reused as a dependency in `snake_arena`.
- **`snake_arena`** — this project. uv workspace with these member packages:
  - `services/sa_common/` — shared types, db layer, queue helpers, bundler
  - `services/runner/` — orchestrates a single match (agent containers, CPU budgets, replay capture)
  - `services/builder/` — turns submitted code into a tagged Docker image
  - `services/orchestrator/` — daemons that poll `match_jobs` / `test_match_jobs` and dispatch work to the runner
  - `services/api/` — FastAPI REST API; thin HTTP layer over `sa_common`
  - `services/submitter/` — CLI tool for submitting agents from outside the browser
  - `sandbox-images/python/` — Python base sandbox image (harness, gRPC stubs)
  - `frontend/` — React + TypeScript SPA (Vite, Clerk, Monaco editor, live replay viewer)
  - `sim-image/` — containerized `snake_sim`
  - `proto/` — shared gRPC contracts

## What is working end-to-end

- Python sandbox base image with harness + generated gRPC stubs + `manifest.toml` for language metadata. Hardened: non-root user, no caps, `--cpus=1.0`.
- Sim image (containerized `snake_sim`) talks gRPC to agents.
- Builder service: takes user code + language → produces a tagged dev image.
- Runner service: takes N submission images + sim image, creates per-agent isolated Docker networks, runs the match, captures replay via `FilePersistObserver`, enforces CPU + wall-clock budgets, returns a `MatchResult`.
- `CpuBudgetObserver` / `AgentContainerManager`: per-step CPU, sustained CPU, per-step wall-clock (contention-adaptive), and sustained wall-clock budgets. Kill reasons are wired all the way through to `match_participants.killed_by_budget` and the dev console banner.
- Match bundle: replay JSONL + analysis JSON + exec times + budgets JSON zipped together and stored via the `IBundler` interface (`LocalBundler` for dev, stub for R2).
- Postgres schema (one migration `001.sql`): `users`, `projects`, `match_jobs`, `test_match_jobs`, `matches`, `match_participants`.
- Versioning model: a single `projects` row carries both the iterative dev state (draft code, latest test build image) and the frozen submitted state (pinned code archive, pinned image tag, version counter). No separate `code_versions` or `submissions` tables.
- `match_jobs` queue + orchestrator `match` daemon: polls `match_jobs`, dispatches ranked matches, persists results atomically.
- `test_match_jobs` queue + orchestrator `test-match` daemon: builds dev image inline if needed, runs match, streams live frames over Redis pub/sub.
- FastAPI REST API: project CRUD, file save/submit, test match enqueue, match job enqueue, bundle URL serving, WebSocket live streaming endpoint.
- Clerk JWT auth on all write endpoints; JWKS cached at startup.
- `psycopg_pool.ConnectionPool` in the API for concurrent request handling.
- React frontend: Monaco-based code editor, project/file management, test match launch with live replay, SimPlayer with timeline scrubber and analysis highlights (death/trap markers).
- gVisor (`runsc`) installed and validated in the Ubuntu VM.
- Hostile agent suite (`hostile/`): fork bomb, mem bomb, disk fill, network probe, privesc, cpu spin, kernel-info probe. All confirmed contained by the sandbox.

## What is not yet built

1. **A second language base image** (Rust or Go). Proves the language-agnostic design is real.
2. **Tournament scheduler** — bulk match runs across all current submissions, ELO / ranking computation, leaderboard UI.
3. **Leaderboard / rankings page** in the frontend. Match data and participants are stored; the UI surface isn't built.
4. **Per-agent timing summary** (mean / p95 / max response time per match) in `match_participants.metrics`. Exec times are captured in bundles; summary stats are not computed or persisted.
5. **Cloudflare R2 wiring** — currently replays land on local disk (`LocalBundler`). R2 `IBundler` implementation + env-var wiring for production.
6. **Production deployment** — Hetzner VM setup scripts, nightly `pg_dump` → R2 backup, `logrotate` config, Cloudflare routing.
7. **Image GC policy** — no cron job pruning old dev/submitted images from the Docker daemon yet.
8. **Submission history view** — user can see what version they're on but not browse past submitted versions or their match history.

## Known smaller items

- gVisor only installed in the Ubuntu test VM, not the Arch dev box. Fine; production gets it.
- `final_length` in `match_participants` is always NULL (TODO in `build_participants` — analysis API not yet confirmed).
- No retention / GC policies defined for bundles or container images.
- No rate-limiting (`slowapi`) on the API endpoints yet; Cloudflare layer is the only protection for now.
- Stale `running` jobs: if the orchestrator dies mid-match, the job row stays `running`. The test-match daemon resets stale rows at startup; the ranked match daemon has a manual recovery note but no automatic reaper yet.

## Top-level architecture in one sentence

One VM running everything (Postgres + API + orchestrator daemons + sim/agent containers + reverse proxy), Redis for live WebSocket streaming, Cloudflare R2 for bundles + DB backups, Cloudflare in front for TLS / DDoS, gVisor + container hardening + per-agent networks for sandboxing, per-step CPU and wall-clock budgets enforced by the runner reading cgroups directly.

## Pointers to the other docs

- Architecture and decisions with rationale → `02_architecture_and_decisions.md`
- Subsystem details (runner, builder, sim, observer protocol, CPU budget) → `03_subsystems.md`
- Storage design (the canonical document) → `04_storage_design.md`
- Database schema, migrations, and data-layer conventions → `05_database_and_schema.md`
- Custom instructions / working style → `06_custom_instructions.md`
- API, frontend, and auth decisions → `07_api_frontend_auth.md`
- CPU and wall-clock budget details → `08_cpu_and_wall_budgets.md`
