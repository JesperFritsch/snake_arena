-- migrations/001_initial.sql

-- Types
CREATE TYPE match_mode      AS ENUM ('multiplayer', 'solo');
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
    name                   TEXT NOT NULL,
    language               TEXT NOT NULL,
    source                 project_source NOT NULL,

    -- Dev side: editor draft + most recent test build, all transient
    dev_code_archive       BYTEA,                            -- editor's current draft
    dev_image_tag          TEXT,                             -- last test build, overwritten each time
    dev_build_status       TEXT,                             -- 'saved' | 'building' | 'ready' | 'failed' | NULL
    dev_built_at           TIMESTAMPTZ,

    -- Submitted side: pinned for ranked matches, version-counted
    submitted_code_archive BYTEA,                            -- frozen on each submit
    submitted_image_tag    TEXT,                             -- unique-per-version tag
    submitted_version      INT NOT NULL DEFAULT 0,           -- 0 = never submitted
    submitted_at           TIMESTAMPTZ,

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

-- Matches: the durable record
CREATE TABLE matches (
    id            BIGSERIAL PRIMARY KEY,
    match_uuid    TEXT UNIQUE NOT NULL,
    status        TEXT NOT NULL,                              -- 'success' | 'failure'
    mode          match_mode NOT NULL,
    sim_args      JSONB NOT NULL,                             -- { mode, map_name, grid_width, grid_height, ... }
    started_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at   TIMESTAMPTZ,
    replay_r2_key TEXT,
    error         TEXT,
    is_test       BOOLEAN NOT NULL DEFAULT FALSE              -- TRUE for user-initiated dev test runs
);

-- Participants: who played in a match, and which submitted version of them.
-- project_version is a snapshot of projects.submitted_version at dispatch
-- time. The exact code is gone once a newer submit overwrites
-- submitted_code_archive, but the version number stays — so history reads
-- "v3 of agent X played, won by length 47" forever.
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

-- Job queue
CREATE TABLE match_jobs (
    id            BIGSERIAL PRIMARY KEY,
    status        job_status NOT NULL DEFAULT 'queued',
    project_ids   BIGINT[] NOT NULL,                          -- who plays (by project, not submission)
    sim_args      JSONB NOT NULL,                             -- { mode, map_name, grid_width, grid_height, ... }
    requested_by  BIGINT REFERENCES users(id),
    requested_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at    TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ,
    match_id      BIGINT REFERENCES matches(id),
    error         TEXT
);

-- Test match jobs: user-initiated dev-build matches.
-- player_project_id uses dev_image_tag; opponents use their submitted_image_tag.
-- project_version=0 in match_participants marks the player's dev slot.
CREATE TABLE test_match_jobs (
    id                   BIGSERIAL PRIMARY KEY,
    status               job_status NOT NULL DEFAULT 'queued',
    player_project_id    BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    opponent_project_ids BIGINT[] NOT NULL DEFAULT '{}',
    sim_args             JSONB NOT NULL,
    requested_by         BIGINT REFERENCES users(id),
    requested_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at           TIMESTAMPTZ,
    finished_at          TIMESTAMPTZ,
    match_id             BIGINT REFERENCES matches(id),
    error                TEXT,
    bundle_path          TEXT                                   -- relative path within ARTIFACTS_DIR, e.g. test-matches/{id}/bundle.zip
);

-- Indexes

CREATE INDEX idx_match_jobs_queued
    ON match_jobs(requested_at) WHERE status = 'queued';

CREATE INDEX idx_match_participants_project
    ON match_participants(project_id);

CREATE INDEX idx_match_participants_project_version
    ON match_participants(project_id, project_version);

CREATE INDEX idx_test_match_jobs_queued
    ON test_match_jobs(requested_at) WHERE status = 'queued';