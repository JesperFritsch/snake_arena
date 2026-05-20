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