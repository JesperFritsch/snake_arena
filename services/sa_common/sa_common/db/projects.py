"""Data layer for the projects table.

A project carries two parallel sets of build state on a single row:

  dev_*       — the iterative test cycle. Save updates dev_code_archive,
                test-builds update dev_image_tag. No version counter.

  submitted_* — the deliberate "I want this to compete" act. Submit
                promotes dev → submitted (copying the archive and the dev
                image tag) and bumps submitted_version.

The dev side is transient: code is overwritten on every save. The dev image,
however, is expected to be tagged immutably per build by the builder (e.g.
snake-dev-{project_id}:{build_job_id}) rather than overwritten under one
reused tag. That immutability is what lets submit simply *copy* dev_image_tag
onto the submitted side: a later test build produces a new, differently-named
image, so a previously-submitted version keeps pointing at the exact image it
was promoted from. `match_participants.project_version` preserves which
version played in any past match, even after the dev code archive is gone.

For listings, prefer ProjectMeta queries to avoid pulling code bytes you
don't need.
"""
from __future__ import annotations

import argparse
import gzip
import io
import sys
import tarfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from psycopg import Connection
from psycopg.rows import class_row, dict_row

from sa_common.db.connection import get_conn


ProjectSource = Literal["browser", "external_image"]
BuildStatus = Literal["saved", "building", "ready", "failed"]


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
class PublicProjectSummary:
    """Lightweight public view of a submitted project, safe to expose to any user."""
    id: int
    name: str
    language: str
    submitted_version: int
    submitted_at: datetime
    user_display_name: str


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


def list_all_submitted(conn: Connection) -> list[PublicProjectSummary]:
    """All projects with a submitted version, any user — for opponent selection."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT p.id, p.name, p.language, p.submitted_version, p.submitted_at,
                   u.display_name AS user_display_name
            FROM projects p
            JOIN users u ON u.id = p.user_id
            WHERE p.submitted_version > 0
            ORDER BY p.submitted_at DESC
            """
        )
        return [
            PublicProjectSummary(
                id=row["id"],
                name=row["name"],
                language=row["language"],
                submitted_version=row["submitted_version"],
                submitted_at=row["submitted_at"],
                user_display_name=row["user_display_name"],
            )
            for row in cur.fetchall()
        ]


# --------------------------------------------------------------------------
# SAVE — editor save, browser projects only
# --------------------------------------------------------------------------

def restore_dev_from_submitted(conn: Connection, project_id: int) -> bool:
    """Copy submitted_code_archive back to dev_code_archive.

    Resets dev_build_status to NULL — the dev code has changed so any
    previous build is stale. Returns True if updated, False if the project
    has never been submitted (nothing to restore from).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE projects
            SET dev_code_archive = submitted_code_archive,
                dev_build_status = 'saved',
                updated_at       = NOW()
            WHERE id = %s
              AND submitted_version > 0
            RETURNING id
            """,
            (project_id,),
        )
        return cur.fetchone() is not None


def get_project_names(conn: Connection, project_ids: list[int]) -> dict[int, str]:
    """Return {project_id: name} for the given IDs. Missing IDs are omitted."""
    if not project_ids:
        return {}
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT id, name FROM projects WHERE id = ANY(%s)", (project_ids,))
        return {row["id"]: row["name"] for row in cur.fetchall()}


def project_name_exists(conn: Connection, name: str) -> bool:
    """True if a project with this exact name already exists (names are global)."""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM projects WHERE name = %s", (name,))
        return cur.fetchone() is not None


def delete_project(conn: Connection, project_id: int) -> bool:
    """Delete a project row. Returns True if deleted, False if not found.

    Raises psycopg.errors.ForeignKeyViolation if the project is referenced
    by match_participants (i.e. it has match history). The API layer should
    catch this and return a 409 rather than silently swallowing the error.
    """
    with conn.cursor() as cur:
        cur.execute("DELETE FROM projects WHERE id = %s RETURNING id", (project_id,))
        return cur.fetchone() is not None


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
            SET dev_code_archive = %s, dev_build_status = 'saved', updated_at = NOW()
            WHERE id = %s AND source = 'browser'
            """,
            (dev_code_archive, project_id),
        )


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
    """Record a successful COMPILE — the image exists but is not yet validated.

    Status becomes 'built', NOT 'ready': a build that compiles may still crash
    before it can play. Only a test run where the dev agent reaches the match
    promotes it to 'ready' (see record_dev_build_validated). Submit requires
    'ready', so a 'built' (un-validated) image cannot be submitted.

    `dev_image_tag` should be unique per build (carry the build's uuid). Submit
    copies it onto the submitted side, so a submitted version keeps pointing at
    the exact image it was promoted from even after later test builds.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE projects
            SET dev_image_tag = %s,
                dev_build_status = 'built',
                dev_built_at = NOW()
            WHERE id = %s
            """,
            (dev_image_tag, project_id),
        )


def record_dev_build_validated(conn: Connection, project_id: int) -> None:
    """The dev agent reached the match (survived construction + init + the
    startup cpu/mem budget). The build is now submittable: status 'ready'."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE projects SET dev_build_status = 'ready' WHERE id = %s",
            (project_id,),
        )


def record_dev_build_crashed(conn: Connection, project_id: int) -> None:
    """The dev agent did not reach the match — it crashed or was killed (cpu/mem)
    before the first update(). Status 'crashed': compiled but not submittable."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE projects SET dev_build_status = 'crashed' WHERE id = %s",
            (project_id,),
        )


def record_dev_build_failure(conn: Connection, project_id: int) -> None:
    """Record a failed COMPILE. Leaves dev_image_tag at its previous value
    so an earlier successful build remains usable until the next success."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE projects SET dev_build_status = 'failed' WHERE id = %s",
            (project_id,),
        )


# --------------------------------------------------------------------------
# SUBMIT — promote dev to submitted, bump version. Deliberate user action.
#
# This is a pure DB operation: no Docker, no retag. It copies the current dev
# image tag onto the submitted side. The API (and any other caller) can run
# this without container-runtime access — it only ever touches DB rows.
# --------------------------------------------------------------------------

def promote_to_submitted(conn: Connection, project_id: int) -> int | None:
    """Promote the current dev build to submitted and bump version.

    Copies dev_code_archive and dev_image_tag onto the submitted columns. No
    image retag happens here; correctness depends on dev_image_tag being
    immutable per build (see record_dev_build_success).

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
                submitted_image_tag    = dev_image_tag,
                submitted_version      = submitted_version + 1,
                submitted_at           = NOW()
            WHERE id = %s
              AND dev_build_status = 'ready'
              AND dev_built_at >= updated_at
            RETURNING submitted_version
            """,
            (project_id,),
        )
        row = cur.fetchone()
        return row[0] if row else None


# --------------------------------------------------------------------------
# Archive helpers
# --------------------------------------------------------------------------

def _safe_arcname(path: str) -> str:
    """Normalise a client-supplied relative path into a safe tar member name.

    The browser sends whatever paths its editor holds; the builder later runs
    extractall() on the archive we produce, so a bad path here is a write
    outside the build dir. Reject absolute paths and any traversal, collapse
    redundant separators, and forbid empties.
    """
    cleaned = path.strip().replace("\\", "/")
    if cleaned.startswith("/"):
        raise ValueError(f"absolute paths not allowed: {path!r}")
    parts = [seg for seg in cleaned.split("/") if seg not in ("", ".")]
    if not parts:
        raise ValueError(f"empty or invalid path: {path!r}")
    if any(seg == ".." for seg in parts):
        raise ValueError(f"path escapes archive root: {path!r}")
    return "/".join(parts)


def pack_files(files: Iterable[tuple[str, bytes]]) -> bytes:
    """Pack (relative_path, content) pairs into a deterministic .tar.gz blob.

    Paths are sanitised (see _safe_arcname) and de-duplicated. Members are
    sorted and timestamps zeroed so the same file set always produces the same
    bytes — useful for hashing / change detection.
    """
    seen: set[str] = set()
    entries: list[tuple[str, bytes]] = []
    for path, data in files:
        arcname = _safe_arcname(path)
        if arcname in seen:
            raise ValueError(f"duplicate path: {arcname!r}")
        seen.add(arcname)
        entries.append((arcname, data))
    entries.sort(key=lambda e: e[0])

    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w") as tar:
        for arcname, data in entries:
            info = tarfile.TarInfo(name=arcname)
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))

    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as gz:
        gz.write(tar_buf.getvalue())
    return out.getvalue()


def unpack_files(data: bytes) -> list[tuple[str, bytes]]:
    """Inverse of pack_files: a .tar.gz blob -> sorted (path, content) pairs.

    Only regular files are returned (directory entries are implied by paths).
    """
    files: list[tuple[str, bytes]] = []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            extracted = tar.extractfile(member)
            files.append((member.name, extracted.read() if extracted else b""))
    files.sort(key=lambda e: e[0])
    return files


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


def read_template_files(
    language: str, templates_dir: str | Path
) -> list[tuple[str, bytes]]:
    """Read a language's starter template from {templates_dir}/{language}/ as
    (relative_path, bytes). Paths are relative to the language dir and
    forward-slashed. Returns [] if the directory is missing.
    """
    base = Path(templates_dir) / language
    if not base.is_dir():
        return []
    files: list[tuple[str, bytes]] = []
    for p in sorted(base.rglob("*")):
        if not p.is_file():
            continue
        files.append((p.relative_to(base).as_posix(), p.read_bytes()))
    return files


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
                new_version = promote_to_submitted(conn, args.project_id)
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