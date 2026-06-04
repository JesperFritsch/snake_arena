# Storage Design

**Status:** Draft v1
**Last updated:** 2026-05-13

This document captures the storage architecture for gridsnake and the reasoning behind each decision. The v1 design optimizes for cost, simplicity, and being run by one developer on one VM.

## Goals

- **Cost ceiling:** the system must run for under 200 SEK/month at hobby scale.
- **No surprise bills:** egress costs must be bounded.
- **Operational simplicity:** one developer should be able to operate the entire stack without paging.
- **Recoverability:** any single-machine failure should be recoverable from off-box backups in under an hour.
- **No unnecessary lock-in:** every storage choice should have an upgrade path to a more sophisticated solution if the platform grows.

These goals push toward "one VM doing most things, with durable blobs offloaded to cheap object storage." Premature distribution is explicitly rejected.

## Data categories

Five categories of data exist in the system. They differ in shape, durability requirements, access patterns, and lifecycle.

### 1. Relational data — structured facts

Users, projects, code versions, submissions, matches, match participants, tournaments. These are the canonical records of "what exists" and "what happened." Small per row, frequently queried, must never be silently lost.

Storage: **PostgreSQL**, running in a container on the VM.

Actual schema (see `05_database_and_schema.md` and `migrations/001.sql` for details):

```
users(id, clerk_user_id, email, display_name, created_at)
projects(id, user_id, name, language, source,
         dev_code_archive, dev_image_tag, dev_build_status, dev_built_at,
         submitted_code_archive, submitted_image_tag, submitted_version, submitted_at,
         created_at, updated_at)
matches(id, match_uuid, status, mode, sim_args, started_at, finished_at,
        bundle_key, error, is_test)
match_participants(match_id, seat, project_id, project_version,
                   final_length, fatal_step, survival_rank,
                   killed_by_budget, metrics)
match_jobs(id, status, project_ids, sim_args, requested_by, requested_at,
           started_at, finished_at, match_id, error)
test_match_jobs(id, status, player_project_id, opponent_project_ids, sim_args,
                requested_by, requested_at, started_at, finished_at,
                match_id, error, bundle_key, pinned)
```

Notable design choices that diverged from earlier plans:

- **No separate `code_versions` or `submissions` tables.** Both the iterative dev state and the frozen submitted state live on the `projects` row. `submitted_version` (integer counter) is the cross-match identity for history. This is simpler and sufficient for v1.
- **`bundle_key` on `matches` / `test_match_jobs`, not a separate `match_artifacts` table.** One key per match, references a zip in the bundler store. Simpler than a join table; the zip itself contains replay, analysis, exec times, and logs.
- **Source code lives in `projects.dev_code_archive` (BYTEA).** Same rationale as the original plan: small, high-value, transactional with project metadata.

### 2. Container images — submission artifacts

Every accepted submission produces a Docker image (~100 MB after layer dedup). Many images accumulate over time.

Storage: **Local Docker daemon on the VM** for v1. No dedicated registry until needed.

Rationale:
- Runner and builder live on the same host. Local daemon is reachable, free, and zero-config.
- Garbage collection is straightforward: keep last N submissions per project, prune older. Cron job.
- When the platform grows to multiple hosts, migrate to a private registry (self-hosted `registry:2`, GitHub Container Registry, or similar). The migration is mechanical; no architectural change needed.

### 3. Replay files — match recordings

One `.run_proto` per match. KB to low-MB each. Written once at match end, read occasionally (replay viewer, analysis), kept indefinitely or until garbage-collected.

Storage: **Cloudflare R2** from day one (currently `LocalBundler` on disk for dev).

Rationale:
- **Egress is free on R2.** Replays will be served to browsers; replay-heavy users could otherwise burn the VM's bandwidth budget. Single biggest cost-protection decision in v1.
- **Decoupled from VM lifecycle.** If the VM is destroyed, replays persist.
- **Browser can fetch directly via presigned URL.** Saves the VM from proxying replay bytes on every view.

Layout (actual):
```
matches/{match-uuid}/bundle.zip        # ranked match bundles
test-matches/{job-id}/bundle.zip       # test match bundles
```

Each `bundle.zip` contains `replay.json` (JSONL), `analysis.json`, `exec_times.json`, `budgets.json`, and optionally `agent_logs.json` (dev test matches).

Date-keyed paths were considered but the match UUID / job ID is sufficient for addressing; date prefixes would add complexity with no operational benefit at v1.

### 4. Analysis output and highlights

Derived from the replay file via the existing `run_analyzer` tool. Typically smaller than the replay itself (KB scale). Same write-once/read-occasionally pattern.

Bundled inside the match's `bundle.zip` as `analysis.json` — not a separate R2 object. Colocated with the replay it was derived from.

Rationale:
- Same access pattern as the replay — always fetched together (the frontend loads the bundle once and reads both files from the zip).
- Keeping it in Postgres would mean the schema changes as the analyzer's output evolves. Storing as JSON in the zip keeps the DB schema stable.
- Trivially regeneratable from the replay if ever lost.

### 5. Operational logs

Sim logs, agent logs, build logs, runner logs. Useful for debugging recent failures, low long-term value.

Storage: **Local disk on the VM**, rotated by `logrotate`.

Rationale:
- Logs are mostly accessed when troubleshooting something that just happened. Remote log services add operational weight without paying off at this scale.
- Rotation prevents disk fill.
- When the platform grows or a real incident needs forensics, a centralized log service (Loki, Vector, etc.) can be added. Not v1.

## What is explicitly not stored

- **Editor drafts** that haven't been explicitly saved. Browser holds pre-save state.
- **Live match state.** Ephemeral. The replay is the durable record.
- **Per-user warm sandbox containers.** Created and destroyed per editor session.
- **Build caches.** Docker handles its own layer cache.
- **Container metadata duplicated from Docker.** The Docker daemon is the source of truth for image existence; the database stores only the tag string.

The general principle: persist only what is canonical or expensive to recompute. Derived data lives in object storage if expensive to regenerate, or nowhere at all if it isn't.

## Where everything lives

### On the Hetzner VM
```
/var/lib/snake-arena/
├── postgres-data/        # Docker volume for postgres
├── logs/                 # rotated service logs
└── (Docker daemon storage for images via /var/lib/docker)
/etc/snake-arena/         # config files, .env, secrets
```

### In Cloudflare R2
```
matches/<match-uuid>/bundle.zip
test-matches/<job-id>/bundle.zip
backups/postgres/<yyyy-mm-dd>.dump
```

### Not on the VM (deliberately)

- Anything user-facing that produces egress (replays, analysis) — those live in R2.
- Long-term backups — R2 again.

## Backup strategy

- **Postgres:** `pg_dump` nightly to `r2://snake-arena/backups/postgres/<date>.dump`. Retain 30 daily, 12 monthly. Shell script + cron.
- **Container images:** not backed up. Submissions can be rebuilt from `code_versions.code_blob` if needed.
- **Replays + analysis:** R2 is the canonical store. R2 has its own redundancy; no separate backup.
- **Code in `code_versions`:** included in the Postgres dump.

A complete VM loss is recoverable in this order:

1. Reprovision a new VM via Hetzner.
2. Run `scripts/setup-host.sh` (Docker + gVisor + ufw + etc.).
3. Restore latest Postgres dump from R2.
4. Rebuild base sandbox images.
5. Optionally re-build needed submission images from `code_versions` rows.
6. Replays and analysis already in R2 — nothing to restore.

Estimated full recovery time: under one hour.

## Database choice: Postgres vs. SQLite

Considered SQLite for simplicity but chose Postgres.

Reasons for Postgres:
- Concurrent writes from runner + API + builder are real even at v1. SQLite locks the whole DB on writes.
- Standard tooling (`pg_dump`, monitoring, migration tools) is mature.
- No architectural change needed if/when scaling to managed Postgres.
- Docker-based setup is one line.

Reasons SQLite was considered:
- Zero operational weight.
- Single file, trivially copied.
- Adequate performance for low concurrency.

The Postgres operational overhead at v1 is low. The asymmetry between "Postgres handles future scaling fine" and "SQLite would need replacement at some point" tipped the decision.

## Object storage choice: R2 vs. S3

Considered S3 but chose Cloudflare R2.

Reasons for R2:
- **Zero egress fees.** Decisive factor for serving replays to browsers.
- Already in use by Jesper's other services — known quantity.
- API-compatible with S3, so libraries (`boto3`, etc.) work unchanged.
- Generous free tier (10 GB storage, ~10 M class-A ops/month).

Reasons S3 was considered:
- Tighter integration with AWS-native services we might use later.
- More mature ecosystem.

Neither outweighs egress cost.

## Cost expectations

At Scenario A (10-200 users):
- Postgres: $0 (runs on the VM).
- Replays + analysis in R2: typically free.
- Backups: small. Free tier covers it.
- **Storage cost contribution: effectively $0.**

At Scenario C (2000+ users):
- Postgres still local; ~$15/month if migrated to managed.
- R2 storage grows but stays under $5/month even with hundreds of GB.
- **Storage cost contribution: under $20/month.**

## Migration paths

| Trigger | Migration |
|---|---|
| Postgres becomes ops burden | Move to managed Postgres. Schema unchanged. |
| Multiple runner hosts | Stand up self-hosted Docker registry; switch image refs from local tags to registry URLs. |
| Need centralized logs | Run Loki or Vector on the VM. |
| Storage growth makes pruning urgent | Add lifecycle policies on R2. |
| Multi-region or HA required | Significant work; consider separately. Not anticipated. |

Each transition is independently triggerable. No flag day.

## Open questions

- **Retention policy for replays.** Keep all forever? GC after N months? Tier to cold storage? Wait for usage data.
- **Code version retention.** Every save, or only submitted versions?
- **Tournament data shape.** Schema is provisional.
- **Image GC policy.** "Last N per project" with N=5 or 10 to start.

## Summary of decisions

| Concern | Decision | Reason |
|---|---|---|
| Relational data | Postgres on the VM | Standard, scales out cleanly later |
| User source code | Postgres column on `code_versions` | Small, transactional with project |
| Container images | Local Docker daemon | Single-host, zero config |
| Replays | Cloudflare R2 | Free egress, decoupled from VM |
| Analysis output | Cloudflare R2 | Same as replays |
| Logs | Local disk + logrotate | Local debugging, low value |
| Backups | Nightly `pg_dump` to R2 | Cheap, recoverable in <1 hour |
