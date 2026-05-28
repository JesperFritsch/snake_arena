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

## Current schema (migration `001.sql`)

The full schema lives in `migrations/001.sql`. Key tables:

**`users`** — Clerk-provisioned identity. `clerk_user_id` (unique) links to Clerk; `id` is the FK used everywhere internally.

**`projects`** — One row per agent. Carries both the iterative dev state and the frozen submitted state:
- `dev_code_archive`, `dev_image_tag`, `dev_build_status`, `dev_built_at` — mutable; overwritten on each save/build.
- `submitted_code_archive`, `submitted_image_tag`, `submitted_version`, `submitted_at` — frozen on each submit; only moved forward, never backward.
- `submitted_version = 0` means never submitted.

**`matches`** — One row per completed (or failed) match. `bundle_key` is the bundler storage key for the zip (e.g. `matches/{uuid}/bundle.zip`). `is_test = TRUE` for user-initiated dev runs (excluded from leaderboard).

**`match_participants`** — One row per agent per match. `seat` is the runner-assigned slot (per-match disambiguator, not a cross-match identity). `project_version` snapshots `projects.submitted_version` at dispatch time — so history reads "version 3 of agent X played" forever, even after the user submits version 4. `killed_by_budget` is set from the runner's `CpuBudgetObserver`. `metrics` is a JSONB catch-all for future per-participant stats (currently `{}`).

**`match_jobs`** — Ranked match queue. `project_ids` (array) is resolved to current submitted images at dispatch time, not at enqueue time. `FOR UPDATE SKIP LOCKED` for concurrent orchestrator safety.

**`test_match_jobs`** — Dev test match queue. `player_project_id` uses `dev_image_tag`; `opponent_project_ids` use their `submitted_image_tag`. `bundle_key` on this row (not on `matches`) because test bundles are pruned independently. `pinned = TRUE` prevents pruning.

### `seat` vs `project_id` — cross-match identity

`seat` is per-match (position 0, 1, 2...). Cross-match identity is `(project_id, project_version)`. To find all matches a specific version of an agent played: `SELECT * FROM match_participants WHERE project_id = $1 AND project_version = $2`.

## Migrations philosophy

- Plain SQL files in `migrations/`, numbered: `001.sql`, `002.sql`, ...
- Applied manually with `psql -f migrations/NNN.sql`.
- **All pre-launch changes go into `001.sql`** — no new migration files until the site is live and has real data that must be preserved. After launch, append-only numbered files.
- No Alembic / Yoyo / Sqitch. Reach for a tool only when actually-deployed instances with data we can't lose exist.

## Data layer

Lives in `services/sa_common/sa_common/db/`. Module layout:

```
sa_common/
├── types.py                # MatchResult, ParticipantRow, SimArgs, RunAnalysis, ...
└── db/
    ├── connection.py       # get_conn(autocommit=...) → psycopg.Connection
    ├── matches.py          # record_match_result(), get_match(), list_matches(), ...
    ├── match_jobs.py       # enqueue/claim/mark for ranked match queue
    ├── test_match_jobs.py  # enqueue/claim/mark/prune for test match queue
    ├── projects.py         # CRUD, save_dev_code(), promote_to_submitted(), ...
    └── users.py            # get_or_create_user(), ...
```

### Connection model

The connection helper (`get_conn`) returns a `psycopg.Connection` with `autocommit=True` by default. Callers wrap atomic operations in `with conn.transaction():` blocks. This means:

- No implicit transaction wrapping every statement.
- The match itself (slow, multi-minute) runs outside any transaction — no row locks held.
- DB writes (claim job, record result + flip status) are each their own explicit atomic block.

The API uses `psycopg_pool.ConnectionPool` for concurrent request handling. Orchestrator daemons open one long-lived connection per process (no pool needed — they're single-threaded iteration loops).

### Survival rank computation

Rank 1 = died last (or alive at match end). Computed in `runner/match_results.py`:

```python
def death_order_key(sid):
    return fatal_steps.get(sid, final_step + 1)  # alive-at-end → beyond any real fatal step

ranked = sorted(snake_ids, key=death_order_key, reverse=True)
rank_of_snake = {sid: i + 1 for i, sid in enumerate(ranked)}
```

## Connection pooling

Not yet. Runner opens one connection per match write. When the API layer arrives and we have concurrent requests, introduce `psycopg_pool.ConnectionPool`. Don't pre-optimize.

## ORM

None. Direct SQL with `psycopg`'s parameterized queries. ORMs (SQLAlchemy) have value, but at this size they obscure more than they help.

## Secrets

- Dev: password in `docker-compose.yml` is fine.
- Hetzner: environment variables sourced from `.env` files outside the repo.
- `.env` is gitignored; `.env.example` is committed showing only variable names.
