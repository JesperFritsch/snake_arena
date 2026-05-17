# Database, Schema, and Migrations

## Setup

Postgres 16 in a Docker container on the dev machine. `docker-compose.yml` at repo root:

```yaml
services:
  postgres:
    image: postgres:16
    container_name: snake-arena-postgres
    restart: unless-stopped
    environment:
      POSTGRES_USER: snake_arena
      POSTGRES_PASSWORD: dev_password_change_me
      POSTGRES_DB: snake_arena
    ports:
      - "127.0.0.1:5432:5432"     # localhost only — important
    volumes:
      - ./pg-data:/var/lib/postgresql/data
```

The `127.0.0.1:` prefix is non-negotiable: without it, Docker exposes the port on *all* interfaces. Same rule applies on Hetzner.

Connection string for dev: `postgresql://snake_arena:dev_password_change_me@localhost:5432/snake_arena`.

`DATABASE_URL` is read from the environment (`.env` file, gitignored; `.env.example` committed).

## Library

`psycopg` v3 (not v2). Installed via the `[binary]` extra so libpq is bundled:

```toml
dependencies = [
    "psycopg[binary]>=3.2",
]
```

## Migrations philosophy

- Plain SQL files in `migrations/`, numbered: `001_initial.sql`, `002_add_tournaments.sql`, ...
- Applied manually with `psql -f migrations/NNN_<name>.sql` for v1.
- One file = one logical change. Append-only history of structural changes.
- **Migrations are not for changing data.** Rare exceptions (seeding reference data) get their own numbered file.
- **No editing applied migrations.** Once it has run on any environment, it is frozen. Mistakes get fixed by a *new* numbered migration.
- No Alembic / Yoyo / Sqitch / Goose yet. Reach for one only when actually-deployed instances with data we can't lose exist.

### Column-drop workflow specifically

Postgres handles `ALTER TABLE ... DROP COLUMN` cleanly — it's a metadata-only operation; disk space frees gradually as rows are touched. Three things matter:

1. It is irreversible. If the data might be wanted later, copy it elsewhere first.
2. Application code that references the dropped column will error after the migration runs. Update code in the same commit as the migration.
3. For production deployments later: deploy code that doesn't use the column first, then apply the migration. No mismatched moment.

## Current schema (migration `001_initial.sql`)

```sql
CREATE TABLE matches (
    id              BIGSERIAL PRIMARY KEY,
    match_uuid      TEXT UNIQUE NOT NULL,        -- the runner's match_id string
    status          TEXT NOT NULL,                -- 'success' | 'failure'
    sim_exit_code   INT,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at     TIMESTAMPTZ,
    replay_r2_key   TEXT,                         -- can be a local path for now
    error           TEXT
);

CREATE TABLE match_participants (
    match_id         BIGINT NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    slot             INT NOT NULL,                 -- snake_id from sim
    agent_name       TEXT NOT NULL,                -- "agent1", etc.
    final_length     INT,
    fatal_step       INT,
    survival_rank    INT,
    killed_by_budget BOOLEAN DEFAULT FALSE,
    PRIMARY KEY (match_id, slot)
);
```

### `slot` is the sim's `snake_id`

This was raised in conversation as potentially confusing — *isn't a submission going to have a different snake_id across different matches?* Yes, and that's fine. `slot` is **per-match**: it labels which position in *this* match a participant occupied. To find "all matches of this submission," join `match_participants → submissions` once the `submissions` table exists and a `submission_id` FK is added to `match_participants`. The slot is not the cross-match identity.

## Planned future migrations (rough order)

1. **`002_add_submissions.sql`** — `submissions(id, project_id, code_version_id, image_tag, status, language, created_at)`, plus `submission_id FK` on `match_participants`.
2. **`003_add_users_projects_code_versions.sql`** — three related tables. Filling in the chain `User ─< Project ─< CodeVersion ─< Submission`.
3. **`004_add_timing_columns.sql`** — `total_cpu_ms`, `mean_step_ms`, `p95_step_ms`, `max_step_ms` on `match_participants`. Computed by the extended `run_analyzer`.
4. **`005_add_trap_columns.sql`** — `trap_count`, `trapped_count` on `match_participants`.
5. **`006_add_jobs.sql`** — the job-queue table. `(id, kind, status, payload, created_at, started_at, finished_at, error, priority)`.
6. **`007_add_tournaments.sql`** — `tournaments`, `tournament_matches`. Shape provisional until the feature is built.
7. **`008_add_sessions_or_auth.sql`** — depends on auth model.

## Data layer

Lives in `services/sa_common/sa_common/db/`. Module layout:

```
sa_common/
├── types.py            # MatchResult and other shared dataclasses
└── db/
    ├── __init__.py     # re-exports
    ├── connection.py   # get_conn(), transaction()
    ├── matches.py      # record_match_result(), ...
    ├── submissions.py  # (future)
    └── users.py        # (future)
```

### Connection helper

```python
# sa_common/db/connection.py
import logging, os
from contextlib import contextmanager
from typing import Iterator
import psycopg

log = logging.getLogger(__name__)

def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL not set.")
    return url

def get_conn() -> psycopg.Connection:
    """Open a new connection. Caller closes it (or use `transaction`)."""
    return psycopg.connect(_database_url())

@contextmanager
def transaction() -> Iterator[psycopg.Connection]:
    """
    Open a connection, run the block inside a transaction,
    commit on success, rollback on any exception, always close.
    """
    conn = get_conn()
    try:
        with conn:           # commits on success, rolls back on exception
            yield conn
    except Exception:
        log.exception("transaction failed; rolled back")
        raise
    finally:
        conn.close()
```

### Match write

```python
# sa_common/db/matches.py — abbreviated
def record_match_result(
    conn: psycopg.Connection,
    match_uuid: str,
    result: MatchResult,
    started_at: datetime,
    finished_at: datetime | None = None,
) -> int:
    if finished_at is None:
        finished_at = datetime.now(timezone.utc)

    status = "success" if result.success else "failure"
    replay_key = str(result.replay_path) if result.replay_path else None

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO matches (
                match_uuid, status, sim_exit_code,
                started_at, finished_at, replay_r2_key, error
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (match_uuid, status, result.sim_exit_code,
             started_at, finished_at, replay_key, result.error),
        )
        match_id = cur.fetchone()[0]

        participants = _derive_participants(result)
        if participants:
            cur.executemany(
                """
                INSERT INTO match_participants (
                    match_id, slot, agent_name,
                    final_length, fatal_step, survival_rank, killed_by_budget
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (match_id, p["slot"], p["agent_name"],
                     p["final_length"], p["fatal_step"],
                     p["survival_rank"], p["killed_by_budget"])
                    for p in participants
                ],
            )
    return match_id
```

`_derive_participants` is best-effort. If `result.run_analysis` is missing (catastrophic sim failure), participants are still emitted from `result.agent_logs.keys()` with nulls.

### Survival rank computation

```python
def death_order_key(sid: int) -> int:
    return fatal_steps.get(sid, final_step + 1)  # alive-at-end → effectively last

ranked = sorted(snake_ids, key=death_order_key, reverse=True)
rank_of = {sid: i + 1 for i, sid in enumerate(ranked)}
# rank 1 = died last (or alive at match end)
```

### Runner integration

After computing `result`, the runner writes best-effort:

```python
try:
    with transaction() as conn:
        record_match_result(conn, match_id, result, started_at=started_at)
except Exception as e:
    log.exception("failed to record match result: %s", e)
```

Match data is also in the artifacts dir, so a DB outage is not catastrophic — but it does need attention.

## Connection pooling

Not yet. Runner opens one connection per match write. When the API layer arrives and we have concurrent requests, introduce `psycopg_pool.ConnectionPool`. Don't pre-optimize.

## ORM

None. Direct SQL with `psycopg`'s parameterized queries. ORMs (SQLAlchemy) have value, but at this size they obscure more than they help.

## Secrets

- Dev: password in `docker-compose.yml` is fine.
- Hetzner: environment variables sourced from `.env` files outside the repo.
- `.env` is gitignored; `.env.example` is committed showing only variable names.
