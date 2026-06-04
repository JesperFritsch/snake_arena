-- migrations/001_initial.sql

-- Types
CREATE TYPE job_status      AS ENUM ('queued', 'running', 'success', 'failure', 'cancelled');
CREATE TYPE project_source  AS ENUM ('browser', 'external_image');

-- Identity
CREATE TABLE users (
    id            BIGSERIAL PRIMARY KEY,
    clerk_user_id TEXT UNIQUE NOT NULL,
    email         TEXT UNIQUE NOT NULL,
    display_name  TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Projects: one row per agent. Carries both the "dev" (iterative test) and
-- "submitted" (ranked) state. Save updates dev_code_archive. Test-builds
-- update dev_image_tag. Submit promotes dev -> submitted and bumps version.
CREATE TABLE projects (
    id                     BIGSERIAL PRIMARY KEY,
    user_id                BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name                   VARCHAR(32) NOT NULL,
    language               TEXT NOT NULL,
    source                 project_source NOT NULL,

    -- Dev side: editor draft + most recent test build, all transient
    dev_code_archive       BYTEA,                            -- editor's current draft
    dev_image_tag          TEXT,                             -- last test build, overwritten each time
    dev_build_status       TEXT,                             -- 'saved'|'building'|'built'|'ready'|'crashed'|'failed'|NULL; 'ready' (validated by a test run) is the only submittable state
    dev_built_at           TIMESTAMPTZ,

    -- Submitted side: pinned for ranked matches, version-counted
    submitted_code_archive BYTEA,                            -- frozen on each submit
    submitted_image_tag    TEXT,                             -- unique-per-version tag
    submitted_version      INT NOT NULL DEFAULT 0,           -- 0 = never submitted
    submitted_at           TIMESTAMPTZ,
    submitted_crashed      BOOLEAN NOT NULL DEFAULT FALSE,   -- set when the submitted image fails gRPC init in any match; cleared by promote_to_submitted on next submit

    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (name),

    -- Browser projects must have dev code; external-image projects must not.
    CONSTRAINT projects_dev_code_matches_source CHECK (
        (source = 'browser'        AND dev_code_archive IS NOT NULL) OR
        (source = 'external_image' AND dev_code_archive IS NULL)
    ),

    -- Submitted side: either fully present or fully absent. Prevents partial
    -- promotions where, say, the image_tag is set but the version isn't.
    CONSTRAINT projects_submitted_consistent CHECK (
        (submitted_version = 0
            AND submitted_code_archive IS NULL
            AND submitted_image_tag IS NULL
            AND submitted_at IS NULL)
        OR
        (submitted_version > 0
            AND submitted_image_tag IS NOT NULL
            AND submitted_at IS NOT NULL
            AND (source = 'external_image' OR submitted_code_archive IS NOT NULL))
    ),

    CONSTRAINT projects_dev_code_size CHECK (
        dev_code_archive IS NULL OR octet_length(dev_code_archive) < 5 * 1024 * 1024
    ),
    CONSTRAINT projects_submitted_code_size CHECK (
        submitted_code_archive IS NULL OR octet_length(submitted_code_archive) < 5 * 1024 * 1024
    )
);

-- Mode groups: a leaderboard-facing label that bundles modes together. The
-- canonical case is solo: a group "solo" contains several solo modes that
-- differ in map / dimensions but share a tab in the leaderboard and a single
-- contribution to the overall normalised score. Modes can be ungrouped
-- (group_slug NULL) — those render as their own standalone tab.
CREATE TABLE mode_groups (
    slug         TEXT PRIMARY KEY,                 -- e.g. 'solo'
    name         TEXT NOT NULL,                    -- display name, e.g. 'Solo'
    description  TEXT,
    sort_order   INT NOT NULL DEFAULT 0,           -- tab order; lower comes first
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Modes: persistent evaluation configurations. Each ranked match belongs to
-- exactly one mode. Test matches have mode_id = NULL. See docs/09_ranking_system.md.
CREATE TABLE modes (
    id                          BIGSERIAL PRIMARY KEY,
    slug                        TEXT UNIQUE NOT NULL,         -- e.g. 'multi-4-standard'
    name                        TEXT NOT NULL,                -- display name
    description                 TEXT,
    group_slug                  TEXT REFERENCES mode_groups(slug) ON DELETE SET NULL,
    participant_count           INT NOT NULL,                 -- 1 for solo, 2+ for multi
    sim_args                    JSONB NOT NULL,               -- {food, grid_width, grid_height}
    map_slug                    TEXT,                         -- NULL = clear map (no walls); maps not yet implemented
    avg_budget_ms               DOUBLE PRECISION NOT NULL,    -- sustained average CPU budget (= cgroup refill rate); the per-step peak is avg_budget_ms × sa_common.scoring.PER_STEP_BUDGET_MULTIPLIER
    scoring_kind                TEXT NOT NULL,                -- 'multi' | 'solo'; categories are canonical per kind in sa_common.scoring
    target_matches_per_version  INT NOT NULL,
    enabled                     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT modes_participant_count_positive CHECK (participant_count >= 1),
    CONSTRAINT modes_target_positive            CHECK (target_matches_per_version >= 1),
    CONSTRAINT modes_budget_positive            CHECK (avg_budget_ms > 0),
    CONSTRAINT modes_scoring_kind_valid         CHECK (scoring_kind IN ('multi', 'solo'))
);

CREATE INDEX idx_modes_group_slug ON modes(group_slug) WHERE group_slug IS NOT NULL;

-- Matches: the durable record
CREATE TABLE matches (
    id                  BIGSERIAL PRIMARY KEY,
    match_uuid          TEXT UNIQUE NOT NULL,
    status              TEXT NOT NULL,                              -- 'success' | 'failure'
    mode_id             BIGINT REFERENCES modes(id) ON DELETE SET NULL, -- NULL for test matches
    sim_args            JSONB NOT NULL,                             -- snapshot of what the runner used
    started_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at         TIMESTAMPTZ,
    bundle_key          TEXT,                                       -- bundler storage key, e.g. matches/{uuid}/bundle.zip
    error               TEXT,
    is_test             BOOLEAN NOT NULL DEFAULT FALSE              -- TRUE for user-initiated dev test runs
);

-- Participants: who played in a match, and which submitted version of them.
-- project_version is a snapshot of projects.submitted_version at dispatch
-- time. The exact code is gone once a newer submit overwrites
-- submitted_code_archive, but the version number stays — so history reads
-- "v3 of agent X played, won by length 47" forever.
--
-- `metrics` JSONB carries base per-snake facts the runner writes:
-- start_length, steps_alive, avg_cpu_ms, trapping_count. final_length and
-- survival_rank live as native columns (canonical) and are NOT also in
-- metrics. There are no scorer-written fields here — scoring is computed
-- on demand by sa_common.db.agent_scores.compute_mode_scores.
CREATE TABLE match_participants (
    match_id         BIGINT NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    seat             INT NOT NULL,                            -- runner-assigned slot; just a disambiguator
    project_id       BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    project_version  INT NOT NULL,                            -- snapshot of projects.submitted_version
    final_length     INT,
    fatal_step       INT,
    survival_rank    INT,                                     -- meaningless for solo (always 1)
    killed_by_budget BOOLEAN NOT NULL DEFAULT FALSE,
    metrics          JSONB NOT NULL DEFAULT '{}',
    PRIMARY KEY (match_id, seat)
);

-- Aggregate scores are computed on demand from match_participants by
-- sa_common.db.agent_scores.compute_mode_scores — there is no agent_scores
-- table. See docs/09_ranking_system.md.

-- Job queue. mode_id is the mode this job belongs to; the runner copies it
-- onto the resulting match row so the scorer knows which config to apply.
CREATE TABLE match_jobs (
    id            BIGSERIAL PRIMARY KEY,
    status        job_status NOT NULL DEFAULT 'queued',
    mode_id       BIGINT NOT NULL REFERENCES modes(id) ON DELETE RESTRICT,
    project_ids   BIGINT[] NOT NULL,                          -- who plays (by project, not submission); not FK-enforced
    sim_args      JSONB NOT NULL,                             -- snapshot of mode.sim_args at enqueue time
    requested_by  BIGINT REFERENCES users(id) ON DELETE SET NULL,
    requested_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at    TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ,
    match_id      BIGINT REFERENCES matches(id) ON DELETE SET NULL,
    error         TEXT
);

-- Test match jobs: user-initiated dev-build matches.
-- player_project_id uses dev_image_tag; opponents use their submitted_image_tag.
-- project_version=0 in match_participants marks the player's dev slot.
CREATE TABLE test_match_jobs (
    id                   BIGSERIAL PRIMARY KEY,
    status               job_status NOT NULL DEFAULT 'queued',
    player_project_id    BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    opponent_project_ids BIGINT[] NOT NULL DEFAULT '{}',     -- not FK-enforced
    sim_args             JSONB NOT NULL,
    requested_by         BIGINT REFERENCES users(id) ON DELETE SET NULL,
    requested_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at           TIMESTAMPTZ,
    finished_at          TIMESTAMPTZ,
    match_id             BIGINT REFERENCES matches(id) ON DELETE SET NULL,
    error                TEXT,
    bundle_key           TEXT,                                  -- bundler storage key, e.g. test-matches/{id}/bundle.zip
    pinned               BOOLEAN NOT NULL DEFAULT FALSE          -- kept regardless of retention pruning
);

-- Indexes

CREATE INDEX idx_match_jobs_queued
    ON match_jobs(requested_at) WHERE status = 'queued';

CREATE INDEX idx_match_jobs_mode_queued
    ON match_jobs(mode_id) WHERE status = 'queued';

CREATE INDEX idx_match_participants_project
    ON match_participants(project_id);

CREATE INDEX idx_match_participants_project_version
    ON match_participants(project_id, project_version);

CREATE INDEX idx_test_match_jobs_queued
    ON test_match_jobs(requested_at) WHERE status = 'queued';

-- Leaderboard and matchmaker queries hit this constantly.
CREATE INDEX idx_matches_mode_status
    ON matches(mode_id, status) WHERE mode_id IS NOT NULL;

-- Seed groups. Solo gets its own group; multi-* modes start ungrouped (each
-- is its own tab). Add solo modes via `db_modes create --group-slug solo ...`
-- once map support lands in the sim (see docs/09_ranking_system.md "Forward path").
INSERT INTO mode_groups (slug, name, description, sort_order) VALUES
    ('solo', 'Solo', 'Single-snake runs across a rotating set of maps.', 100);

-- Seed modes. scoring_kind selects the canonical category list (see
-- sa_common.scoring.CANONICAL_*_CATEGORIES) — adding a kind means adding
-- a category list there.
INSERT INTO modes
    (slug,                name,                  description,                                  participant_count, sim_args,                                                              avg_budget_ms, scoring_kind, target_matches_per_version)
VALUES
    ('multi-4-standard',  '4-player Standard',   'Full-board scrum on a 20x15 board.',         4,                 '{"start_length": 1, "food": 7, "grid_width": 20, "grid_height": 15}', 10,            'multi',      5),
    ('multi-6-standard',  '6-player Standard',   'Full-board scrum on a 20x20 board.',         6,                 '{"start_length": 1, "food": 10, "grid_width": 20, "grid_height": 20}', 10,            'multi',      5),
    ('solo-mini-items',   'Solo Mini Items',     'Single snake on the mini_items map.',        1,                 '{"start_length": 1, "map": "mini_items", "food": 10}',                10,            'solo',       2),
    ('solo-mini-stairs',  'Solo Mini Stairs',    'Single snake on the mini_stairs map.',       1,                 '{"start_length": 1, "map": "mini_stairs", "food": 10}',               10,            'solo',       2);

UPDATE modes SET group_slug = 'solo' WHERE slug IN ('solo-mini-items', 'solo-mini-stairs');

-- --------------------------------------------------------------------------
-- LISTEN/NOTIFY wakeups for event-driven daemons. Triggers fire inside the
-- transaction of the row change, so by the time the daemon receives the
-- notification, the row is committed and visible. See docs/09_ranking_system.md.
-- --------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION fn_notify() RETURNS TRIGGER AS $$
BEGIN
    PERFORM pg_notify(TG_ARGV[0], '');
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

-- Match runner: wake on newly queued ranked match job.
CREATE TRIGGER trg_match_jobs_queued
    AFTER INSERT ON match_jobs
    FOR EACH ROW WHEN (NEW.status = 'queued')
    EXECUTE FUNCTION fn_notify('match_runner_wakeup');

-- Test runner: wake on newly queued test match job.
CREATE TRIGGER trg_test_match_jobs_queued
    AFTER INSERT ON test_match_jobs
    FOR EACH ROW WHEN (NEW.status = 'queued')
    EXECUTE FUNCTION fn_notify('test_runner_wakeup');

-- Scheduler: wake on signals that change "what should be queued":
--   - new submission (more underplayed work)
--   - mode added/enabled
--   - queue slot drained (a queued job moved to running/completed)
--   - new ranked success match (changes underplay measurements)
CREATE TRIGGER trg_projects_submitted
    AFTER UPDATE OF submitted_version ON projects
    FOR EACH ROW WHEN (NEW.submitted_version > OLD.submitted_version)
    EXECUTE FUNCTION fn_notify('scheduler_wakeup');

CREATE TRIGGER trg_modes_inserted_enabled
    AFTER INSERT ON modes
    FOR EACH ROW WHEN (NEW.enabled = TRUE)
    EXECUTE FUNCTION fn_notify('scheduler_wakeup');

CREATE TRIGGER trg_modes_enabled
    AFTER UPDATE OF enabled ON modes
    FOR EACH ROW WHEN (NEW.enabled = TRUE AND OLD.enabled = FALSE)
    EXECUTE FUNCTION fn_notify('scheduler_wakeup');

CREATE TRIGGER trg_match_jobs_drained
    AFTER UPDATE OF status ON match_jobs
    FOR EACH ROW WHEN (OLD.status = 'queued' AND NEW.status <> 'queued')
    EXECUTE FUNCTION fn_notify('scheduler_wakeup');

CREATE TRIGGER trg_matches_completed
    AFTER INSERT ON matches
    FOR EACH ROW WHEN (NEW.status = 'success' AND NEW.mode_id IS NOT NULL)
    EXECUTE FUNCTION fn_notify('scheduler_wakeup');

-- No agent_scores invalidation trigger: aggregates are computed on demand
-- by compute_mode_scores, which filters to mp.project_version =
-- p.submitted_version. Bumping submitted_version simply makes prior-version
-- matches stop counting on the next leaderboard read.