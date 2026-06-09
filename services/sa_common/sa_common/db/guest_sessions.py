"""DB layer for guest_sessions.

Guest sessions let unauthenticated visitors try the editor and run up to
GUEST_TEST_LIMIT test matches before being prompted to sign in. All session
data (projects, test-match bundles, Docker images) is cleaned up 48 hours
after the session expires.

Ownership model
---------------
A project row is owned by either a user_id (signed-in) or a guest_session_id
(anonymous). Exactly one is non-NULL; the DB CHECK enforces this.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.rows import class_row

GUEST_TEST_LIMIT = 10


@dataclass(slots=True)
class GuestSession:
    session_id: str
    test_count: int
    created_at: datetime
    expires_at: datetime


def get_or_create_guest_session(
    conn: psycopg.Connection, session_id: str
) -> GuestSession:
    """Return the existing session or create a fresh one.

    Also refreshes expires_at to now + 48 h so active sessions don't expire
    while the user is still working.
    """
    with conn.cursor(row_factory=class_row(GuestSession)) as cur:
        cur.execute(
            """
            INSERT INTO guest_sessions (session_id)
            VALUES (%s)
            ON CONFLICT (session_id) DO UPDATE
                SET expires_at = GREATEST(
                    guest_sessions.expires_at,
                    NOW() + INTERVAL '48 hours'
                )
            RETURNING session_id, test_count, created_at, expires_at
            """,
            (session_id,),
        )
        row = cur.fetchone()
        assert row is not None
        return row


def get_guest_session(
    conn: psycopg.Connection, session_id: str
) -> GuestSession | None:
    with conn.cursor(row_factory=class_row(GuestSession)) as cur:
        cur.execute(
            """
            SELECT session_id, test_count, created_at, expires_at
            FROM guest_sessions WHERE session_id = %s
            """,
            (session_id,),
        )
        return cur.fetchone()


def increment_guest_test_count(
    conn: psycopg.Connection, session_id: str
) -> GuestSession | None:
    """Atomically increment test_count. Returns None if the session is gone."""
    with conn.cursor(row_factory=class_row(GuestSession)) as cur:
        cur.execute(
            """
            UPDATE guest_sessions
            SET test_count = test_count + 1
            WHERE session_id = %s
            RETURNING session_id, test_count, created_at, expires_at
            """,
            (session_id,),
        )
        return cur.fetchone()


def claim_guest_session(
    conn: psycopg.Connection, session_id: str, user_id: int
) -> int:
    """Migrate all guest projects to user_id, then delete the session.

    Returns the number of projects migrated.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE projects
            SET user_id = %s, guest_session_id = NULL
            WHERE guest_session_id = %s
            """,
            (user_id, session_id),
        )
        migrated = cur.rowcount
        cur.execute(
            "DELETE FROM guest_sessions WHERE session_id = %s",
            (session_id,),
        )
    return migrated


@dataclass(slots=True)
class _ExpiredSessionData:
    session_id: str
    image_tags: list[str]
    bundle_keys: list[str]


def collect_and_delete_expired_sessions(
    conn: psycopg.Connection,
) -> tuple[list[str], list[str]]:
    """Delete all expired guest sessions and return artefacts to clean up.

    Returns (image_tags, bundle_keys). The caller is responsible for removing
    Docker images and bundle files — the DB rows are already gone when this
    returns.
    """
    with conn.cursor() as cur:
        # Collect image tags for expired guest projects.
        cur.execute(
            """
            SELECT p.dev_image_tag
            FROM projects p
            JOIN guest_sessions gs ON gs.session_id = p.guest_session_id
            WHERE gs.expires_at < NOW()
              AND p.dev_image_tag IS NOT NULL
            """
        )
        image_tags = [row[0] for row in cur.fetchall()]

        # Collect bundle keys for test matches belonging to expired guest projects.
        cur.execute(
            """
            SELECT DISTINCT tmj.bundle_key
            FROM test_match_jobs tmj
            JOIN projects p ON p.id = tmj.player_project_id
            JOIN guest_sessions gs ON gs.session_id = p.guest_session_id
            WHERE gs.expires_at < NOW()
              AND tmj.bundle_key IS NOT NULL
            """
        )
        bundle_keys = [row[0] for row in cur.fetchall()]

        # Delete expired sessions — cascades to projects → test_match_jobs.
        cur.execute("DELETE FROM guest_sessions WHERE expires_at < NOW()")
        deleted = cur.rowcount

    if deleted:
        import logging
        logging.getLogger(__name__).info(
            "cleaned up %d expired guest sessions (%d images, %d bundles)",
            deleted, len(image_tags), len(bundle_keys),
        )

    return image_tags, bundle_keys
