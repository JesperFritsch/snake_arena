"""Data layer for the users table."""
from __future__ import annotations

import argparse
import sys

from dataclasses import dataclass
from datetime import datetime

from psycopg import Connection
from psycopg.rows import class_row

from sa_common.db.connection import get_conn


@dataclass
class User:
    id: int
    clerk_user_id: str
    email: str
    display_name: str
    created_at: datetime


def create_user(
    conn: Connection, clerk_user_id: str, email: str, display_name: str
) -> int:
    """Insert a new user. Returns the new user's id."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO users (clerk_user_id, email, display_name)
            VALUES (%s, %s, %s)
            RETURNING id
            """,
            (clerk_user_id, email, display_name),
        )
        row = cur.fetchone()
        assert row is not None
        return row[0]


def get_user(conn: Connection, user_id: int) -> User | None:
    with conn.cursor(row_factory=class_row(User)) as cur:
        cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
        return cur.fetchone()


def get_user_by_email(conn: Connection, email: str) -> User | None:
    with conn.cursor(row_factory=class_row(User)) as cur:
        cur.execute("SELECT * FROM users WHERE email = %s", (email,))
        return cur.fetchone()


def get_user_by_clerk_id(conn: Connection, clerk_user_id: str) -> User | None:
    with conn.cursor(row_factory=class_row(User)) as cur:
        cur.execute(
            "SELECT * FROM users WHERE clerk_user_id = %s", (clerk_user_id,)
        )
        return cur.fetchone()


@dataclass
class UserDeletionArtifacts:
    """Out-of-transaction cleanup work left over after a user delete.

    The DB cascade leaves orphans in the bundle store (nginx/R2) and the
    Docker daemon — neither is reachable inside the SQL transaction. The
    caller is responsible for deleting these best-effort after the
    transaction commits. If those secondary deletes fail, re-running the
    cleanup is a no-op (DB row already gone) but doesn't retry them —
    image GC is expected to mop up later.
    """
    found: bool
    bundle_keys: list[str]
    image_tags: list[str]


def delete_user_by_clerk_id(
    conn: Connection, clerk_user_id: str
) -> UserDeletionArtifacts:
    """Erase a user and everything that personally identifies them.

    Cascade does the heavy lifting:
      users -> projects (CASCADE)
              -> match_participants (CASCADE)
              -> test_match_jobs (CASCADE, via player_project_id)
      users -> match_jobs.requested_by (SET NULL)
              -> test_match_jobs.requested_by (SET NULL)

    On top of that we explicitly delete `matches` rows that no longer have
    any participants (i.e. every seat belonged to the deleted user), so
    we don't leave bare match rows behind. The scheduler will re-balance
    surviving opponents' underplay counts on its next wakeup.

    Idempotent: if the user is already gone, returns found=False with empty
    artifact lists.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM users WHERE clerk_user_id = %s", (clerk_user_id,)
        )
        row = cur.fetchone()
        if row is None:
            return UserDeletionArtifacts(found=False, bundle_keys=[], image_tags=[])
        user_id = row[0]

        # Image tags to log for operator cleanup (Docker daemon is not
        # reachable from the DB transaction).
        cur.execute(
            """
            SELECT tag FROM (
                SELECT dev_image_tag       AS tag FROM projects WHERE user_id = %s
                UNION ALL
                SELECT submitted_image_tag AS tag FROM projects WHERE user_id = %s
            ) t WHERE tag IS NOT NULL
            """,
            (user_id, user_id),
        )
        image_tags = [r[0] for r in cur.fetchall()]

        # Bundle keys for test-match jobs owned by this user (each test job
        # has at most one bundle, owned solely by the player).
        cur.execute(
            """
            SELECT tmj.bundle_key
            FROM test_match_jobs tmj
            JOIN projects p ON p.id = tmj.player_project_id
            WHERE p.user_id = %s AND tmj.bundle_key IS NOT NULL
            """,
            (user_id,),
        )
        bundle_keys = [r[0] for r in cur.fetchall()]

        # Matches that will become orphans (every participant belongs to
        # this user). Collect their ids + bundle keys before deleting; we
        # use the ids again after the cascade to drop the matches rows.
        cur.execute(
            """
            SELECT m.id, m.bundle_key
            FROM matches m
            WHERE EXISTS (
                SELECT 1 FROM match_participants mp
                JOIN projects p ON p.id = mp.project_id
                WHERE mp.match_id = m.id AND p.user_id = %s
            )
            AND NOT EXISTS (
                SELECT 1 FROM match_participants mp2
                JOIN projects p2 ON p2.id = mp2.project_id
                WHERE mp2.match_id = m.id AND p2.user_id <> %s
            )
            """,
            (user_id, user_id),
        )
        orphan_match_rows = cur.fetchall()
        orphan_match_ids = [r[0] for r in orphan_match_rows]
        bundle_keys.extend(r[1] for r in orphan_match_rows if r[1] is not None)

        # Cancel any queued/running ranked match jobs that reference this
        # user's projects in their project_ids array. The array isn't
        # FK-enforced, so the cascade doesn't touch these; the runner
        # would try to dispatch and fail. Mark them cancelled so the
        # scheduler can re-fill.
        cur.execute(
            """
            UPDATE match_jobs
            SET status = 'cancelled', finished_at = NOW(),
                error = 'requesting user deleted'
            WHERE status IN ('queued', 'running')
              AND project_ids && (SELECT ARRAY_AGG(id) FROM projects WHERE user_id = %s)
            """,
            (user_id,),
        )

        # Erase the user. Cascade fires here.
        cur.execute("DELETE FROM users WHERE id = %s", (user_id,))

        # Drop orphan matches. Their match_participants rows are already
        # gone via the cascade; match_jobs.match_id was SET NULL by FK.
        if orphan_match_ids:
            cur.execute(
                "DELETE FROM matches WHERE id = ANY(%s)",
                (orphan_match_ids,),
            )

    return UserDeletionArtifacts(
        found=True, bundle_keys=bundle_keys, image_tags=image_tags
    )


def get_or_create_user_by_clerk_id(
    conn: Connection,
    clerk_user_id: str,
    email: str,
    display_name: str,
) -> User:
    """Resolve a Clerk identity to a local user row, creating it on first sight.

    Keyed on clerk_user_id (the stable Clerk `sub`), not email — emails can
    change and aren't guaranteed stable across providers. Uses an upsert so two
    concurrent first-requests for the same new user can't race into a duplicate.
    """
    with conn.cursor(row_factory=class_row(User)) as cur:
        cur.execute(
            """
            INSERT INTO users (clerk_user_id, email, display_name)
            VALUES (%s, %s, %s)
            ON CONFLICT (clerk_user_id) DO UPDATE
                SET email = EXCLUDED.email,
                    display_name = EXCLUDED.display_name
            RETURNING *
            """,
            (clerk_user_id, email, display_name),
        )
        user = cur.fetchone()
        assert user is not None
        return user


def cli(argv) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers: argparse._SubParsersAction = parser.add_subparsers(dest="command", required=True, help="command to run")
    create_parser: argparse.ArgumentParser = subparsers.add_parser("create")
    create_parser.add_argument("clerk_user_id", help="Clerk user id (sub)")
    create_parser.add_argument("email", help="User email")
    create_parser.add_argument("display_name", help="Display name")

    get_parser: argparse.ArgumentParser = subparsers.add_parser("get")
    mutg = get_parser.add_mutually_exclusive_group(required=True)
    mutg.add_argument("-u", "--user_id", help="User ID")
    mutg.add_argument("-e", "--email", help="Email")
    return parser.parse_args(argv)


def main():
    args = cli(sys.argv[1:])
    with get_conn(autocommit=True) as conn:
        with conn.transaction():
            if args.command == "create":
                user_id = create_user(
                    conn, args.clerk_user_id, args.email, args.display_name
                )
                user = get_user(conn, user_id)
            else:
                if args.user_id:
                    user = get_user(conn, args.user_id)
                else:
                    user = get_user_by_email(conn, args.email)
        print(user)

if __name__ == "__main__":
    main()