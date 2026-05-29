# Ranking system

How ranked matches get scheduled, run, scored, and presented on the leaderboard.

## Goals

1. **Variety.** Players should be tested against multiple configurations — different
   grid sizes, different obstacle layouts, different player counts.
2. **Fairness per submission version.** Each submitted version of a project gets
   played enough times in each configuration to produce a stable score, with
   varied opponents (not the same group every time).
3. **No churn.** If nothing has changed (no new submissions, every existing
   submission has its target matches), the system sits idle without burning compute.
4. **Composable.** Adding a new mode is a database row, not a code change.

---

## Modes

A **mode** is one persistent evaluation configuration. Each mode owns its
ruleset, its target matches-per-version, its scoring weights, and its own
leaderboard. The `modes` table is the source of truth for everything the
scheduler and scorer do.

```sql
modes (
  id                          BIGSERIAL PRIMARY KEY,
  slug                        TEXT UNIQUE NOT NULL,     -- e.g. 'multi-4-standard'
  name                        TEXT NOT NULL,            -- display name
  description                 TEXT,
  participant_count           INT NOT NULL,             -- 1 for solo, 2+ for multi
  sim_args                    JSONB NOT NULL,           -- {food, grid_width, grid_height}
  map_slug                    TEXT,                     -- NULL = clear map (no walls)
  budget_ms                   DOUBLE PRECISION NOT NULL,-- per-step CPU budget
  scoring_config              JSONB NOT NULL,           -- {alpha, beta, w, floor_ms}
  target_matches_per_version  INT NOT NULL,
  enabled                     BOOLEAN NOT NULL DEFAULT TRUE,
  created_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
)
```

A match belongs to at most one mode:

```sql
matches.mode_id BIGINT NULL REFERENCES modes(id) ON DELETE SET NULL
```

`mode_id IS NULL` denotes a test match — those still get scored (see "Test-match
scoring" below) but never appear on the ranked leaderboards.

### Seed modes

The first set of enabled modes:

| slug | participants | grid | food | budget_ms | target | notes |
|---|---|---|---|---|---|---|
| `multi-2-standard` | 2 | 20×20 | 3 | 25 | 15 | head-to-head |
| `multi-4-standard` | 4 | 20×20 | 3 | 25 | 20 | full-board scrum |

Solo modes (e.g. `solo-corridor`, `solo-maze`) get added once map support lands
in the sim — they require obstacle data, which the current `SimArgs` doesn't
carry. The schema is forward-compatible (`map_slug` exists from day one).

---

## Scoring

One parametric formula covers all modes:

```
score = length
      × (1 + β × length / max(steps_alive, 1))        [eating-rate bonus]
      × (1 + α × (1 − (rank − 1) / (n − 1)))          [survival bonus]
      × (budget_ms / max(avg_step_ms, floor_ms)) ^ w  [speed bonus]
```

Where:

- **length** — final snake body length. Raw base — you only grow by eating food.
- **steps_alive** — number of steps the snake stayed alive. `length / steps_alive`
  is the food-eating rate; rewards efficient eating over slow grinding.
- **rank** — survival rank within the match (1 = last alive, n = first to die).
- **avg_step_ms** — mean CPU time per step for this seat across the match.
- **budget_ms** — the per-step budget for this mode.

Per-mode tuning lives in `scoring_config`:

```json
{ "alpha": 0.5, "beta": 2.0, "w": 0.3, "floor_ms": 2.0 }
```

| param | meaning | typical |
|---|---|---|
| `alpha` | survival weight (multi only); 0 in solo modes | 0.5 |
| `beta`  | eating-rate weight | 1.0 – 3.0 |
| `w`     | speed-bonus exponent | 0.3 |
| `floor_ms` | clamp on avg_step_ms; prevents trivial agents gaming the speed bonus | 2.0 |

Solo modes set `alpha = 0`, which collapses the survival factor to 1.

### Anti-camping guarantee

The formula penalises camping (no growth → low `length`, low rate). It also
penalises grinding to outlast opponents without eating (high `steps_alive`,
small `length` → low rate). Outlasting opponents *while eating* is the
strictly-dominant strategy.

### Test matches and scoring

Test matches (`is_test = TRUE`, `mode_id IS NULL`) are **not scored**
today. The scorer's claim query filters `mode_id IS NOT NULL` so it never
touches them, and `test_runner_daemon` doesn't compute scores either.

When test-match scoring is added for dev-agent feedback in the UI, it must
land on a dedicated `test_match_jobs` column (e.g. `scores JSONB`) —
physically separate from ranked match scoring (`match_participants.metrics`).
Sharing the storage location relies on every leaderboard query remembering
`is_test = FALSE`; a separate column makes leakage impossible.

---

## Scheduler

The scheduler picks what to run; it does not pick who plays. It is
event-driven: it walks the enabled modes whenever something happens that
might change "what should be queued" (see "Wakeups" below) and asks the
matchmaker for groups where the mode is **underplayed**.

A `(project, version)` pair is **underplayed** in mode M when its
`matches_played` count (success + in-flight queued/running) is less than
`mode.target`. Same metric for solo and multi modes, and the same metric
the leaderboard's eligibility check uses, so the matchmaker and the
leaderboard agree on "played enough."

For multi modes, the matchmaker *prefers* opponents the seed hasn't played
yet (variety), but doesn't require them. Once every other project has
played the seed at least once, pairings repeat — that's fine, the target
is volume-of-matches, not distinct opponents. A mode with a target higher
than `N - 1` works the same way: each project just plays multiple matches
against each other project until the count reaches target.

In-flight (queued/running) match_jobs are counted toward matches_played
so the matchmaker doesn't fill its per-mode queue cap with copies of the
same project in a single scheduler tick.

Per iteration:

```
for mode in enabled_modes:
    queued_in_mode = count of queued match_jobs for this mode
    if queued_in_mode >= per_mode_queue_cap: continue

    underplayed = versions short of target in this mode
    if underplayed is empty: continue

    if mode is solo:
        enqueue one job per underplayed version (up to remaining queue cap)
    else:
        for each group the matchmaker returns:
            enqueue one job
            stop when queue cap hit
```

If every mode is saturated, the scheduler does no work this tick.

### Queue cap

Lives on the scheduler, not the matchmaker. A simple per-mode cap (e.g. 5)
keeps the runner from being swamped if many submissions arrive at once.

---

## Matchmaker

A pure function. Given a mode and the list of underplayed versions in that
mode, it returns one group of `participant_count` versions to play together.
No queue introspection, no recency window.

For multi modes, the algorithm is a simple greedy:

1. Sort underplayed versions by underplay-degree (most-underplayed first).
2. Seed the group with the top one.
3. Fill remaining seats by walking the rest, preferring versions that
   haven't yet played the seed version in this mode. Break ties by
   underplay-degree, then random.

The "haven't played each other" check is per-mode and per-version:

```sql
SELECT mp2.project_id, mp2.project_version
FROM match_participants mp1
JOIN matches m ON m.id = mp1.match_id AND m.mode_id = $mode
JOIN match_participants mp2 ON mp2.match_id = mp1.match_id AND mp2.seat != mp1.seat
WHERE mp1.project_id = $pid AND mp1.project_version = $ver
  AND m.status = 'success'
```

If no fresh pairing exists (e.g. only 4 versions submitted, target requires
5+ pairings), the matchmaker falls back to repeating the most-underplayed
pairing. Variety is preferred but not enforced — over time, every pairing
fills out.

For solo modes the matchmaker is a no-op: the scheduler enqueues one job
per underplayed version directly.

---

## Scorer

After the runner records a match, `scoring_started_at` and `scored_at` are
both NULL. The scorer:

```
1. CLAIM: SET scoring_started_at = now()
         WHERE scored_at IS NULL AND scoring_started_at IS NULL
         (also reclaim stale leases: scoring_started_at < now() - 5min)
         FOR UPDATE SKIP LOCKED LIMIT 1.
2. FETCH bundle (via IBundler).
3. COMPUTE scores using the mode's scoring_config (default config for test matches).
4. WRITE scores into match_participants.metrics.
5. COMMIT: SET scored_at = now().
```

**Lease, not commit.** If step 2 or 3 fails (transient bundler error, malformed
zip, etc.), the scorer clears `scoring_started_at` and the match goes back in
the pool. This avoids the silent data-loss path where one nginx hiccup
permanently un-scores a match.

On daemon startup, the scorer resets stale leases (`scoring_started_at < now()
- 5 min AND scored_at IS NULL`) so a crashed worker's claims become available
again.

---

## Leaderboards

Two views, both derived from `match_participants.metrics->>'score'`:

### Per-mode

```
SELECT project, AVG(score), MAX(score), AVG(survival_rank), COUNT(*)
FROM match_participants mp JOIN matches m ON m.id = mp.match_id
WHERE m.mode_id = $mode
  AND m.status = 'success'
  AND mp.metrics ? 'score'
GROUP BY project
ORDER BY AVG(score) DESC
```

### Overall

Per-mode scores are not comparable across modes (solo on a 30×30 produces
length-50 snakes; multi-4 on a 10×10 tops out at length 12). The overall
leaderboard normalises each player's per-mode avg to [0, 100] relative to
that mode's leader, then averages across modes.

**Eligibility:** to appear on the overall leaderboard, a project must have
played at least ⌈target / 2⌉ matches in **every** enabled mode. This stops
players who only competed in one easy mode from looking better than players
who competed everywhere.

Overall pseudo-SQL:

```
WITH per_mode AS (
  SELECT mode_id, project_id, AVG(score) AS avg_score, COUNT(*) AS matches
  FROM ranked_participants
  GROUP BY mode_id, project_id
),
mode_leaders AS (
  SELECT mode_id, MAX(avg_score) AS top
  FROM per_mode GROUP BY mode_id
),
normalised AS (
  SELECT pm.project_id, pm.mode_id,
         100 * pm.avg_score / NULLIF(ml.top, 0) AS pct,
         pm.matches
  FROM per_mode pm JOIN mode_leaders ml USING (mode_id)
),
eligible AS (
  SELECT project_id
  FROM normalised n JOIN modes m ON m.id = n.mode_id
  WHERE m.enabled = TRUE
  GROUP BY project_id
  HAVING COUNT(*) FILTER (WHERE n.matches >= CEIL(m.target_matches_per_version::float / 2)) = (SELECT COUNT(*) FROM modes WHERE enabled)
)
SELECT project_id, AVG(pct) AS overall_score
FROM normalised
WHERE project_id IN (SELECT * FROM eligible)
GROUP BY project_id
ORDER BY overall_score DESC
```

---

## Wakeups (LISTEN/NOTIFY)

All four daemons are event-driven — no polling, no intervals. Each one
LISTENs on a Postgres channel via a dedicated connection in a background
thread; the main loop blocks on a `threading.Event` that the listener sets
whenever a notification arrives. Drains all available work, then waits.

The channels and what triggers them (see `migrations/001.sql`):

| Channel | Fired by |
|---|---|
| `match_runner_wakeup` | new `match_jobs` row with status = 'queued' |
| `test_runner_wakeup`  | new `test_match_jobs` row with status = 'queued' |
| `scorer_wakeup`       | new `matches` row with status = 'success' and `mode_id IS NOT NULL` |
| `scheduler_wakeup`    | a project's `submitted_version` increases; a mode is added or enabled; a queued `match_jobs` row leaves the queue; a ranked success match is recorded |

Triggers fire `pg_notify` inside the same transaction as the row change, so
by the time the listener receives the notification the row is committed
and visible to the daemon's work query.

The listener also fires its wakeup once on (re)connect, so a daemon never
misses startup state or events that arrived during a connection blip.

### Scorer retry cap

Without polling, a permanent scorer failure (e.g. a deleted bundle) would
hot-loop. `matches.scoring_attempts` caps retries at 3; after that the
claim query skips the row and it stops being touched until the column is
manually reset.

## Forward path

### Maps (next)

Solo modes need wall/corridor layouts. Plan:

- New `maps` table: `slug, name, width, height, walls JSONB, created_at`.
- Map editor in the frontend (grid canvas; click to toggle wall cells).
- Sim accepts a map by slug; `SimArgs` gains an optional `map_slug` field.
- Seed `solo-corridor`, `solo-maze`, etc. once the loader is in.

### Rating systems (later, maybe never)

Average score is sufficient when the player population is small. If it ever
needs to be order-aware (Elo, Glicko), the formula and scorer plug in here,
not the scheduler.
