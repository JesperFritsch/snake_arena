-- migrations/002_guest_sessions.sql
-- Unauthenticated users get a guest session so they can try the editor and
-- run up to 10 test matches before being prompted to sign in. Sessions and
-- all their artefacts (projects, test-match bundles, Docker images) are
-- cleaned up 48 hours after the session was last refreshed.

CREATE TABLE guest_sessions (
    session_id  UUID        PRIMARY KEY,
    test_count  INT         NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Refreshed on each test-match enqueue; hard-capped at 48 h from creation.
    expires_at  TIMESTAMPTZ NOT NULL DEFAULT NOW() + INTERVAL '48 hours'
);

CREATE INDEX idx_guest_sessions_expires ON guest_sessions(expires_at);

-- Allow a project to be owned by either a user or a guest session (never both,
-- never neither). The NOT NULL constraint on user_id is dropped; the exclusive
-- ownership invariant is enforced by the CHECK below.
ALTER TABLE projects
    ALTER COLUMN user_id DROP NOT NULL,
    ADD COLUMN guest_session_id UUID REFERENCES guest_sessions(session_id) ON DELETE CASCADE;

ALTER TABLE projects
    ADD CONSTRAINT projects_owner_exclusive CHECK (
        (user_id IS NOT NULL AND guest_session_id IS NULL) OR
        (user_id IS NULL     AND guest_session_id IS NOT NULL)
    );

CREATE INDEX idx_projects_guest_session
    ON projects(guest_session_id)
    WHERE guest_session_id IS NOT NULL;
