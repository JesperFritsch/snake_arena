"""Data layer for the projects table.

A project holds a user's current working code as a single tar.gz BYTEA blob
in `code_archive`. The archive is overwritten on save; there is no history.
For listings, prefer ProjectMeta queries to avoid pulling code bytes you
don't need.
"""
from __future__ import annotations

import argparse 
import sys 
import io
import tarfile

from pathlib import Path

from dataclasses import dataclass
from datetime import datetime

from psycopg import Connection
from psycopg.rows import class_row

from sa_common.db.connection import get_conn


@dataclass
class Project:
    """Project including its current code archive."""
    id: int
    user_id: int
    name: str
    language: str
    code_archive: bytes
    created_at: datetime
    updated_at: datetime


@dataclass
class ProjectMeta:
    """Project without the code archive — cheap for listings."""
    id: int
    user_id: int
    name: str
    language: str
    created_at: datetime
    updated_at: datetime


def create_project(
    conn: Connection,
    user_id: int,
    name: str,
    language: str,
    code_archive: bytes,
) -> int:
    """Insert a new project. Returns the new project's id."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO projects (user_id, name, language, code_archive)
            VALUES (%s, %s, %s, %s)
            RETURNING id
            """,
            (user_id, name, language, code_archive),
        )
        row = cur.fetchone()
        assert row is not None
        return row[0]


def update_project_code(conn: Connection, project_id: int, code_archive: bytes) -> None:
    """Overwrite the project's code archive and bump updated_at."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE projects
            SET code_archive = %s, updated_at = NOW()
            WHERE id = %s
            """,
            (code_archive, project_id),
        )


def get_project(conn: Connection, project_id: int) -> Project | None:
    """Fetch a project including its code archive."""
    with conn.cursor(row_factory=class_row(Project)) as cur:
        cur.execute("SELECT * FROM projects WHERE id = %s", (project_id,))
        return cur.fetchone()


def get_project_meta(conn: Connection, project_id: int) -> ProjectMeta | None:
    """Fetch project metadata without the code archive."""
    with conn.cursor(row_factory=class_row(ProjectMeta)) as cur:
        cur.execute(
            """
            SELECT id, user_id, name, language, created_at, updated_at
            FROM projects WHERE id = %s
            """,
            (project_id,),
        )
        return cur.fetchone()


def list_projects_for_user(conn: Connection, user_id: int) -> list[ProjectMeta]:
    """List a user's projects (no code archives), most recently updated first."""
    with conn.cursor(row_factory=class_row(ProjectMeta)) as cur:
        cur.execute(
            """
            SELECT id, user_id, name, language, created_at, updated_at
            FROM projects WHERE user_id = %s
            ORDER BY updated_at DESC
            """,
            (user_id,),
        )
        return cur.fetchall()


def pack_directory(path: str | Path) -> bytes:
    path = Path(path)

    if not path.is_dir():
        raise ValueError(f"{path} is not a directory")

    buffer = io.BytesIO()

    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for file in path.glob("*"):
            tar.add(file, arcname=file.relative_to(path))

    return buffer.getvalue()


def unpack_archive(data: bytes, output_dir: str | Path) -> None:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        tar.extractall(output_dir)



def cli(argv) -> argparse.Namespace:
    parser = argparse.ArgumentParser()

    subparsers: argparse._SubParsersAction = parser.add_subparsers(
        dest="command",
        required=True,
        help="command to run",
    )

    # create
    create_parser = subparsers.add_parser("create")
    create_parser.add_argument("user_id", type=int)
    create_parser.add_argument("name")
    create_parser.add_argument("language")
    create_parser.add_argument("archive_path")

    # get
    get_parser = subparsers.add_parser("get")
    get_parser.add_argument("project_id", type=int)

    # meta
    meta_parser = subparsers.add_parser("meta")
    meta_parser.add_argument("project_id", type=int)

    # list
    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("user_id", type=int)

    # update-code
    update_parser = subparsers.add_parser("update-code")
    update_parser.add_argument("project_id", type=int)
    update_parser.add_argument("archive_path")

    # dump-code
    dump_parser = subparsers.add_parser("dump-code")
    dump_parser.add_argument("project_id", type=int)
    dump_parser.add_argument("output_path")

    return parser.parse_args(argv)


def main():
    args = cli(sys.argv[1:])

    with get_conn(autocommit=False) as conn:
        with conn.transaction():

            if args.command == "create":
                archive = pack_directory(args.archive_path)

                project_id = create_project(
                    conn=conn,
                    user_id=args.user_id,
                    name=args.name,
                    language=args.language,
                    code_archive=archive,
                )

                result = get_project_meta(conn, project_id)

            elif args.command == "get":
                result = get_project(conn, args.project_id)

            elif args.command == "meta":
                result = get_project_meta(conn, args.project_id)

            elif args.command == "list":
                result = list_projects_for_user(conn, args.user_id)

            elif args.command == "update-code":
                archive = pack_directory(args.archive_path)

                update_project_code(
                    conn,
                    args.project_id,
                    archive,
                )

                result = get_project_meta(conn, args.project_id)

            elif args.command == "dump-code":
                project = get_project(conn, args.project_id)

                if project is None:
                    raise SystemExit("project not found")

                unpack_archive(project.code_archive, args.output_path)

                result = f"wrote {len(project.code_archive)} bytes to {args.output_path}"

            else:
                raise AssertionError("unreachable")

    print(result)


if __name__ == "__main__":
    main()

