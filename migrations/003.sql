-- migrations/003_test_match_modes.sql
-- Test matches can run with a ranked mode's config (sim_args + CPU budget)
-- or a custom config, and get scored with the same per-match formula as
-- ranked matches. Scores live here only — never in match_participants
-- aggregation — so test runs can't leak into the leaderboard.

ALTER TABLE test_match_jobs
    -- NULL = custom config. Snapshot columns below hold what was enforced,
    -- so editing or deleting the mode later doesn't rewrite history.
    ADD COLUMN mode_id         BIGINT REFERENCES modes(id) ON DELETE SET NULL,
    ADD COLUMN avg_budget_ms   DOUBLE PRECISION NOT NULL DEFAULT 10,
    -- Dev agent's per-match score (quality × cpu_factor); NULL when the
    -- match couldn't be scored.
    ADD COLUMN score           DOUBLE PRECISION,
    ADD COLUMN score_breakdown JSONB;

CREATE INDEX idx_test_match_jobs_project_mode
    ON test_match_jobs(player_project_id, mode_id, requested_at DESC)
    WHERE score IS NOT NULL;
