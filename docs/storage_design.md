# Storage Design

**Status:** Draft v1
**Last updated:** 2026-05-13

This document captures the storage architecture for gridsnake and the reasoning behind each decision. It is meant to be revisited as the platform grows; the v1 design optimizes for cost, simplicity, and being run by one developer on one VM.

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

Schema sketch (will evolve):
    users(
        id, email, created_at, ...
    )
    projects(
        id, user_id, name, language, created_at
    )
    code_versions(
        id, project_id, version_num, code_blob, created_at,
        parent_version_id  -- for branching history
    )
    submissions(
        id, project_id, code_version_id, image_tag, status, language, created_at
    )  -- status: building | ready | failed | gc'd
    matches(
        id, kind, status, scheduled_at, started_at, finished_at, error
    )  -- kind: test | tournament
        match_participants(
        match_id, slot, submission_id, result, final_score, crashed
    )
        match_artifacts(
        match_id, replay_r2_key, analysis_r2_key, size_bytes, created_at
    )
    tournaments(
        id, name, scheduled_at, status, ruleset
    )
    tournament_matches(
        tournament_id, match_id
    )
    sessions(
        id, user_id, expires_at
    )  -- if session-based auth

Notable design choices:

- **Source code lives in `code_versions.code_blob`.** Source files are small (KB), high-value, and benefit from being transactional with the project metadata. Splitting source into object storage would create consistency problems and add latency for the common case of "show me my project."
- **`match_artifacts` stores R2 keys, not file contents.** Large binary artifacts go to object storage; the DB holds only the reference.
- **Per-table choices defer to the future.** Tournament ruleset, ranking, and similar will solidify when we actually build them.

### 2. Container images — submission artifacts

Every accepted submission produces a Docker image (~100MB after layer dedup). Many images accumulate over time.

Storage: **Local Docker daemon on the VM** for v1. No dedicated registry until needed.

Rationale:
- Runner and builder live on the same host. Local daemon is reachable, free, and zero-config.
- Garbage collection is straightforward: keep last N submissions per project, prune older. Cron job.
- When the platform grows to multiple hosts, migrate to a private registry (self-hosted `registry:2`, GitHub Container Registry, or similar). The migration is mechanical; no architectural change needed.

### 3. Replay files — match recordings

One `run_proto` per match. KB to low-MB each. Written once at match end, read occasionally (replay viewer, analysis), kept indefinitely or until garbage-collected.

Storage: **Cloudflare R2** from day one.

Rationale:
- **Egress is free on R2.** Replays will be served to browsers (replay viewer); replay-heavy users could otherwise burn the VM's bandwidth budget. This is the single biggest cost-protection decision in the v1 design.
- **Decoupled from VM lifecycle.** If the VM is destroyed (Hetzner maintenance, post-compromise rebuild), replays persist independently.
- **Browser can fetch directly via presigned URL.** Saves the VM from proxying replay bytes on every view.

Layout:

snake-arena/
└── replays/<yyyy>/<mm>/<match-id>.run_proto

Date-prefixed paths make bulk operations (e.g., "delete all matches from before 2025") trivial.

### 4. Analysis output and highlights

Derived from the replay file via the existing `run_analyzer` tool. Typically smaller than the replay itself (KB scale). Same write-once/read-occasionally pattern.

Storage: **Cloudflare R2**, same bucket as replays, separate prefix.

snake-arena/
└── analysis/<yyyy>/<mm>/<match-id>.json

Rationale:
- Same access pattern as replays — colocating simplifies retention policies.
- Storing as JSON (not in Postgres) keeps the DB schema stable as the analyzer's output evolves. Adding new highlight types doesn't require migrations.
- Trivially regeneratable from the replay if ever lost.

### 5. Operational logs

Sim logs, agent logs, build logs, runner logs. Useful for debugging recent failures, low long-term value.

Storage: **Local disk on the VM**, rotated by `logrotate`.

Rationale:
- Logs are mostly accessed when troubleshooting something that just happened. Remote log services add operational weight without paying off at this scale.
- Rotation prevents disk fill.
- When the platform grows or when a real incident needs forensics, a centralized log service (Loki, Vector, etc.) can be added. Not v1.

## What is explicitly not stored

- **Editor drafts** that haven't been explicitly saved. Browser holds pre-save state. Reduces write volume on every keystroke and avoids storing throwaway content.
- **Live match state.** Ephemeral. The replay is the durable record.
- **Per-user warm sandbox containers.** Created and destroyed per editor session.
- **Build caches.** Docker handles its own layer cache.
- **Container metadata duplicated from Docker.** The Docker daemon is the source of truth for image existence; the database stores only the tag string.

The general principle: persist only what is canonical or expensive to recompute. Derived data lives in object storage if expensive to regenerate, or nowhere at all if it isn't.

## Where everything lives

### On the Hetzner VM

/var/lib/snake-arena/
├── postgres-data/        # Docker volume for postgres
├── logs/                 # rotated service logs
└── (Docker daemon storage for images via /var/lib/docker)
/etc/snake-arena/         # config files, .env, secrets

### In Cloudflare R2

snake-arena/
├── replays/<yyyy>/<mm>/<match-id>.run_proto
├── analysis/<yyyy>/<mm>/<match-id>.json
└── backups/postgres/<yyyy-mm-dd>.dump

### Not on the VM (deliberately)

- Anything user-facing that produces egress (replays, analysis) — those live in R2.
- Long-term backups — R2 again.

## Backup strategy

- **Postgres:** `pg_dump` nightly to `r2://snake-arena/backups/postgres/<date>.dump`. Retain 30 daily, 12 monthly. Simple shell script + cron.
- **Container images:** not backed up. Submissions can be rebuilt from `code_versions.code_blob` if needed.
- **Replays + analysis:** R2 is the canonical store. R2 has its own redundancy; no separate backup.
- **Code in `code_versions`:** included in the Postgres dump. The text content of user submissions is preserved through the DB backup.

A complete VM loss is recoverable in this order:

1. Reprovision a new VM via Hetzner.
2. Run `scripts/setup-host.sh`.
3. Restore latest Postgres dump from R2.
4. Rebuild base sandbox images.
5. Optionally re-build needed submission images from `code_versions` rows.
6. Replays and analysis already in R2 — nothing to restore.

Estimated full recovery time: under one hour.

## Database choice: Postgres vs. SQLite

Considered SQLite for simplicity but chose Postgres.

Reasons for Postgres:
- Concurrent writes from runner + API + builder are real even at v1. SQLite locks the whole DB on writes, which would cause stalls.
- Standard tooling (`pg_dump`, monitoring, migration tools) is mature.
- No architectural change needed if/when scaling to managed Postgres.
- Docker-based setup is one line.

Reasons SQLite was considered:
- Zero operational weight.
- Single file, trivially copied.
- Adequate performance for low concurrency.

The Postgres operational overhead at v1 is low (one container, one volume, one cron-driven `pg_dump`). The asymmetry between "Postgres handles future scaling fine" and "SQLite would need replacement at some point" tipped the decision.

## Object storage choice: R2 vs. S3

Considered S3 but chose Cloudflare R2.

Reasons for R2:
- **Zero egress fees.** The decisive factor for a system that will serve replays to browsers.
- Already in use by other Jesper-operated services (Instagram pipeline) — known quantity.
- API-compatible with S3, so libraries (`boto3`, etc.) work unchanged.
- Generous free tier (10GB storage, ~10M class-A operations/month) — likely free indefinitely at hobby scale.

Reasons S3 was considered:
- Tighter integration with AWS-native services we might use later.
- More mature ecosystem.

Neither advantage outweighs egress cost. If portions of the system ever require AWS-native services, those can use S3 separately without affecting replay storage.

## Cost expectations

At Scenario A (10-200 users):
- Postgres: $0 (runs on the VM, no separate cost).
- Replays + analysis in R2: typically free (under 10GB and under 10M operations/month).
- Backups: small. Free tier covers it.
- **Storage cost contribution to monthly bill: effectively $0.**

At Scenario C (2000+ users):
- Postgres still local; if migrated to managed, ~$15/month.
- R2 storage grows but stays under $5/month even with hundreds of GB.
- Backup storage similarly small.
- **Storage cost contribution: under $20/month** even at significant scale.

Egress remains negligible because of R2.

## Migration paths

The v1 design is intentionally upgradeable. Likely transitions if/when needed:

| Trigger | Migration |
|---|---|
| Postgres becomes ops burden | Move to managed Postgres (RDS, Azure, Hetzner managed). Schema unchanged. |
| Multiple runner hosts | Stand up self-hosted Docker registry; switch image refs from local tags to registry URLs. |
| Need centralized logs | Run Loki or Vector on the VM; ship logs from each service. |
| Storage growth makes pruning urgent | Add lifecycle policies on R2 (auto-expire old replays). |
| Multi-region or HA required | Significant work; consider separately. Not anticipated. |

Each transition is independently triggerable. No flag day.

## Open questions

Things to revisit when the relevant subsystems are built:

- **Retention policy for replays.** Keep all forever? GC after N months? Tier to cold storage? Decide once we have actual usage data on how often old replays are accessed.
- **Code version retention.** Keep every save, or only "submitted" versions? Probably every save, but small projects with thousands of micro-saves might motivate a cap.
- **Tournament data shape.** Schema sketched above is provisional; refine when tournaments are actually built.
- **Image GC policy.** "Last N per project" is the obvious default. N=5 or 10 is fine to start. Revisit if disk usage on the VM becomes a concern.

## Summary of decisions

| Concern | Decision | Reason |
|---|---|---|
| Relational data | Postgres on the VM | Standard, scales out cleanly later |
| User source code | Postgres column on `code_versions` | Small, transactional with project |
| Container images | Local Docker daemon | Single-host, zero config |
| Replays | Cloudflare R2 | Free egress, decoupled from VM |
| Analysis output | Cloudflare R2 | Same as replays |
| Logs | Local disk + logrotate | Low value long-term |
| Backups | `pg_dump` to R2 nightly | Simple, off-box, sufficient |
| Object storage | Cloudflare R2 over S3 | Egress cost |
| Database | Postgres over SQLite | Concurrent writes, growth path |
| Single VM vs. distributed | Single VM | Sufficient at target scale |