"""Data layer for the projects table.

A project carries two parallel sets of build state on a single row:

  dev_*       — the iterative test cycle. Save updates dev_code_archive,
                test-builds update dev_image_tag. No version counter.

  submitted_* — the deliberate "I want this to compete" act. Submit
                promotes dev → submitted (copying the archive, re-tagging
                the image) and bumps submitted_version.

The dev side is transient: code is overwritten on every save, the image is
overwritten on every test-build. The submitted side is durable enough that
matches always reference a meaningful version number, but the code archive
itself is overwritten on each submit. `match_participants.project_version`
preserves which version played in any past match, even after the code is
gone.

For listings, prefer ProjectMeta queries to avoid pulling code bytes you
don't need.
"""
from __future__ import annotations

import argparse
import io
import sys
import tarfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from psycopg import Connection
from psycopg.rows import class_row

from sa_common.db.connection import get_conn


ProjectSource = Literal["browser", "external_image"]
BuildStatus = Literal["building", "ready", "failed"]


@dataclass
class Project:
    """Full project state including both code archives."""
    id: int
    user_id: int
    name: str
    language: str
    source: str
    dev_code_archive: bytes | None
    dev_image_tag: str | None
    dev_build_status: str | None
    dev_built_at: datetime | None
    submitted_code_archive: bytes | None
    submitted_image_tag: str | None
    submitted_version: int
    submitted_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass
class ProjectMeta:
    """Project metadata without the code archives — cheap for listings and
    for orchestrator dispatch lookups."""
    id: int
    user_id: int
    name: str
    language: str
    source: str
    dev_image_tag: str | None
    dev_build_status: str | None
    dev_built_at: datetime | None
    submitted_image_tag: str | None
    submitted_version: int
    submitted_at: datetime | None
    created_at: datetime
    updated_at: datetime


_META_COLUMNS = """
    id, user_id, name, language, source,
    dev_image_tag, dev_build_status, dev_built_at,
    submitted_image_tag, submitted_version, submitted_at,
    created_at, updated_at
"""


# --------------------------------------------------------------------------
# CRUD
# --------------------------------------------------------------------------

def create_project(
    conn: Connection,
    user_id: int,
    name: str,
    language: str,
    source: ProjectSource = "browser",
    dev_code_archive: bytes | None = None,
) -> int:
    """Insert a new project. Returns the new project's id.

    For browser projects, dev_code_archive must be provided (the editor's
    starting state — typically a language template). For external_image
    projects, dev_code_archive must be None.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO projects (user_id, name, language, source, dev_code_archive)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (user_id, name, language, source, dev_code_archive),
        )
        row = cur.fetchone()
        assert row is not None
        return row[0]


def get_project(conn: Connection, project_id: int) -> Project | None:
    """Fetch a project including both code archives."""
    with conn.cursor(row_factory=class_row(Project)) as cur:
        cur.execute("SELECT * FROM projects WHERE id = %s", (project_id,))
        return cur.fetchone()


def get_project_meta(conn: Connection, project_id: int) -> ProjectMeta | None:
    """Fetch project metadata without the code archives."""
    with conn.cursor(row_factory=class_row(ProjectMeta)) as cur:
        cur.execute(
            f"SELECT {_META_COLUMNS} FROM projects WHERE id = %s",
            (project_id,),
        )
        return cur.fetchone()


def list_projects_for_user(conn: Connection, user_id: int) -> list[ProjectMeta]:
    """List a user's projects (no code archives), most recently updated first."""
    with conn.cursor(row_factory=class_row(ProjectMeta)) as cur:
        cur.execute(
            f"""
            SELECT {_META_COLUMNS} FROM projects WHERE user_id = %s
            ORDER BY updated_at DESC
            """,
            (user_id,),
        )
        return cur.fetchall()


# --------------------------------------------------------------------------
# SAVE — editor save, browser projects only
# --------------------------------------------------------------------------

def save_dev_code(
    conn: Connection, project_id: int, dev_code_archive: bytes
) -> None:
    """Overwrite the project's dev code archive and bump updated_at.

    No-op for non-browser projects (the WHERE clause filters them out).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE projects
            SET dev_code_archive = %s, updated_at = NOW()
            WHERE id = %s AND source = 'browser'
            """,
            (dev_code_archive, project_id),
        )


def submit_project(project_id: int) -> int | None:
    with get_conn() as conn:
        project = get_project_meta(conn, project_id)
        if project is None or project.dev_image_tag is None:
            raise ValueError("project has no dev build to submit")

        new_version = project.submitted_version + 1
        safe_name = "".join(c if c.isalnum() or c in "-_." else "-"
                            for c in project.name.lower())
        new_tag = f"snake-{project.user_id}-{safe_name}:v{new_version}"

        # Re-tag the existing dev image. Fast — just a pointer.
        docker_client = docker.from_env()
        docker_client.images.get(project.dev_image_tag).tag(new_tag)

        # Now the DB. If preconditions fail (dev not ready, code changed),
        # returns None and we should untag.
        result = promote_to_submitted(conn, project_id, new_tag)
        if result is None:
            docker_client.images.get(new_tag).reload()  # untag would be cleaner
            return None

        return result


# --------------------------------------------------------------------------
# TEST BUILD — used by the builder service. Each test-run triggers a build
# that walks: start -> success | failure.
# --------------------------------------------------------------------------

def record_dev_build_start(conn: Connection, project_id: int) -> None:
    """Mark the dev build as in progress."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE projects SET dev_build_status = 'building' WHERE id = %s",
            (project_id,),
        )


def record_dev_build_success(
    conn: Connection, project_id: int, dev_image_tag: str
) -> None:
    """Record a successful dev build. Overwrites dev_image_tag."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE projects
            SET dev_image_tag = %s,
                dev_build_status = 'ready',
                dev_built_at = NOW()
            WHERE id = %s
            """,
            (dev_image_tag, project_id),
        )


def record_dev_build_failure(conn: Connection, project_id: int) -> None:
    """Record a failed dev build. Leaves dev_image_tag at its previous value
    so an earlier successful build remains usable until the next success."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE projects SET dev_build_status = 'failed' WHERE id = %s",
            (project_id,),
        )


# --------------------------------------------------------------------------
# SUBMIT — promote dev to submitted, bump version. Deliberate user action.
# --------------------------------------------------------------------------

def promote_to_submitted(
    conn: Connection,
    project_id: int,
    submitted_image_tag: str,
) -> int | None:
    """Promote the current dev build to submitted and bump version.

    The caller is responsible for retagging the docker image to
    `submitted_image_tag` BEFORE calling this — the DB is the last step so
    that a failed docker retag never leaves a row pointing at a missing
    image. The conventional tag is f"snake-{user_id}-{name}:v{new_version}",
    so the caller needs to peek the current submitted_version first.

    Returns the new submitted_version on success.

    Returns None if the preconditions failed:
      - dev_build_status is not 'ready' (no current dev build), or
      - updated_at > dev_built_at (user saved code after last test build —
        the dev image no longer reflects current code, refuse promotion).

    A None return is a normal outcome, not an error; the UI should tell the
    user to test their changes first.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE projects
            SET submitted_code_archive = dev_code_archive,
                submitted_image_tag    = %s,
                submitted_version      = submitted_version + 1,
                submitted_at           = NOW()
            WHERE id = %s
              AND dev_build_status = 'ready'
              AND dev_built_at >= updated_at
            RETURNING submitted_version
            """,
            (submitted_image_tag, project_id),
        )
        row = cur.fetchone()
        return row[0] if row else None


# --------------------------------------------------------------------------
# Archive helpers — unchanged
# --------------------------------------------------------------------------

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


# --------------------------------------------------------------------------
# CLI — for ops and manual testing
# --------------------------------------------------------------------------

def cli(argv) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser("create")
    create_parser.add_argument("user_id", type=int)
    create_parser.add_argument("name")
    create_parser.add_argument("language")
    create_parser.add_argument("archive_path")

    get_parser = subparsers.add_parser("get")
    get_parser.add_argument("project_id", type=int)

    meta_parser = subparsers.add_parser("meta")
    meta_parser.add_argument("project_id", type=int)

    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("user_id", type=int)

    save_parser = subparsers.add_parser("save-dev")
    save_parser.add_argument("project_id", type=int)
    save_parser.add_argument("archive_path")

    dump_parser = subparsers.add_parser("dump-dev")
    dump_parser.add_argument("project_id", type=int)
    dump_parser.add_argument("output_path")

    dump_sub_parser = subparsers.add_parser("dump-submitted")
    dump_sub_parser.add_argument("project_id", type=int)
    dump_sub_parser.add_argument("output_path")

    submit_parser = subparsers.add_parser("submit")
    submit_parser.add_argument("project_id", type=int)
    submit_parser.add_argument(
        "submitted_image_tag",
        help="The new submitted image tag (caller has already retagged docker)",
    )

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
                    source="browser",
                    dev_code_archive=archive,
                )
                result = get_project_meta(conn, project_id)

            elif args.command == "get":
                result = get_project(conn, args.project_id)

            elif args.command == "meta":
                result = get_project_meta(conn, args.project_id)

            elif args.command == "list":
                result = list_projects_for_user(conn, args.user_id)

            elif args.command == "save-dev":
                archive = pack_directory(args.archive_path)
                save_dev_code(conn, args.project_id, archive)
                result = get_project_meta(conn, args.project_id)

            elif args.command == "dump-dev":
                project = get_project(conn, args.project_id)
                if project is None or project.dev_code_archive is None:
                    raise SystemExit("project not found or has no dev code")
                unpack_archive(project.dev_code_archive, args.output_path)
                result = (
                    f"wrote {len(project.dev_code_archive)} bytes "
                    f"to {args.output_path}"
                )

            elif args.command == "dump-submitted":
                project = get_project(conn, args.project_id)
                if project is None or project.submitted_code_archive is None:
                    raise SystemExit("project not found or never submitted")
                unpack_archive(project.submitted_code_archive, args.output_path)
                result = (
                    f"wrote {len(project.submitted_code_archive)} bytes "
                    f"to {args.output_path}"
                )

            elif args.command == "submit":
                new_version = promote_to_submitted(
                    conn, args.project_id, args.submitted_image_tag
                )
                if new_version is None:
                    raise SystemExit(
                        "submit refused: dev not ready or code changed since "
                        "last test build"
                    )
                result = f"submitted as version {new_version}"

            else:
                raise AssertionError("unreachable")

    print(result)


if __name__ == "__main__":
    main()