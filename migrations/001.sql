-- migrations/001_initial.sql

-- Types
CREATE TYPE match_mode AS ENUM ('multiplayer', 'solo');
CREATE TYPE job_status AS ENUM ('queued', 'running', 'success', 'failure', 'cancelled');

-- Identity chain: users → projects
CREATE TABLE users (
    id           BIGSERIAL PRIMARY KEY,
    email        TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE projects (
    id         BIGSERIAL PRIMARY KEY,
    user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    language   TEXT NOT NULL,
    code_archive BYTEA NOT NULL,                  -- current working draft, overwritten on save
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, name),
    CONSTRAINT projects_code_size CHECK (octet_length(code_archive) < 5 * 1024 * 1024)
);

CREATE TABLE submissions (
    id           BIGSERIAL PRIMARY KEY,
    project_id   BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    image_tag    TEXT UNIQUE NOT NULL,
    code_archive BYTEA NOT NULL,
    status       TEXT NOT NULL,                    -- 'building' | 'ready' | 'failed' | 'gc'd'
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT submissions_code_size CHECK (octet_length(code_archive) < 5 * 1024 * 1024)
);

-- Matches: the durable record
CREATE TABLE matches (
    id            BIGSERIAL PRIMARY KEY,
    match_uuid    TEXT UNIQUE NOT NULL,
    status        TEXT NOT NULL,                   -- 'success' | 'failure'
    mode          match_mode NOT NULL,
    sim_args      JSONB NOT NULL,                 -- how: { mode, map_name, grid_width, grid_height, ... }
    started_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at   TIMESTAMPTZ,
    replay_r2_key TEXT,
    error         TEXT
);

CREATE TABLE match_participants (
    match_id         BIGINT NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    seat             INT NOT NULL,                 -- runner-assigned slot; just a disambiguator
    project_id       BIGINT NOT NULL REFERENCES projects(id),
    submission_id    BIGINT NOT NULL REFERENCES submissions(id),
    final_length     INT,
    fatal_step       INT,
    survival_rank    INT,                          -- meaningless for solo (always 1)
    killed_by_budget BOOLEAN NOT NULL DEFAULT FALSE,
    metrics          JSONB NOT NULL DEFAULT '{}',
    PRIMARY KEY (match_id, seat)
);

-- Job queue: transient input, not duplicated state
CREATE TABLE match_jobs (
    id             BIGSERIAL PRIMARY KEY,
    status         job_status NOT NULL DEFAULT 'queued',
    submission_ids BIGINT[] NOT NULL,              -- who plays
    sim_args       JSONB NOT NULL,                 -- how: { mode, map_name, grid_width, grid_height, ... }
    requested_by   BIGINT REFERENCES users(id),
    requested_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at     TIMESTAMPTZ,
    finished_at    TIMESTAMPTZ,
    match_id       BIGINT REFERENCES matches(id),
    error          TEXT
);

-- Indexes
CREATE INDEX idx_match_jobs_queued ON match_jobs(requested_at) WHERE status = 'queued';
CREATE INDEX idx_match_participants_project    ON match_participants(project_id);
CREATE INDEX idx_match_participants_submission ON match_participants(submission_id);