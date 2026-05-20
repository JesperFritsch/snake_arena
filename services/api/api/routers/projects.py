# services/api/api/routers/projects.py
"""Project endpoints.

A project carries both the dev (iterative test) and submitted (ranked) state
on one row. The API never touches Docker:

  - build  -> insert a row in build_jobs; the builder daemon picks it up.
  - submit -> a pure DB promotion (promote_to_submitted), which copies the
              current dev_image_tag onto the submitted side and bumps the
              version.

Project code crosses the wire as a file structure (a list of {path, content,
encoding}), not a tarball. The API packs it into a .tar.gz for storage and
unpacks it back for the editor, so the browser never handles archive bytes.
"""
from __future__ import annotations

import base64
import binascii
import io

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from psycopg import Connection

from sa_common.db.build_jobs import enqueue_build_job, get_build_job
from sa_common.db.projects import (
    Project,
    ProjectMeta,
    create_project,
    get_project,
    get_project_meta,
    list_projects_for_user,
    pack_files,
    promote_to_submitted,
    save_dev_code,
    unpack_files,
)
from sa_common.db.users import User

from api.auth import get_current_user
from api.db import get_db
from api.schemas import (
    ProjectCreate,
    ProjectFile,
    ProjectFiles,
    SubmitResult,
)

router = APIRouter(prefix="/projects", tags=["projects"])

_MAX_FILES = 2000
_MAX_DECODED_BYTES = 5 * 1024 * 1024   # total uncompressed payload
_MAX_ARCHIVE_BYTES = 5 * 1024 * 1024   # matches the projects_*_code_size CHECKs


def _owned_meta(conn: Connection, project_id: int, user: User) -> ProjectMeta:
    meta = get_project_meta(conn, project_id)
    if meta is None or meta.user_id != user.id:
        # 404 (not 403) so we don't leak which project ids exist.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")
    return meta


def _decode_files(files: list[ProjectFile]) -> list[tuple[str, bytes]]:
    """Decode wire files to (path, bytes), enforcing count/size limits."""
    if len(files) > _MAX_FILES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"too many files (max {_MAX_FILES})",
        )
    decoded: list[tuple[str, bytes]] = []
    total = 0
    for f in files:
        if f.encoding == "base64":
            try:
                data = base64.b64decode(f.content, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"file {f.path!r}: invalid base64 ({exc})",
                ) from exc
        else:
            data = f.content.encode("utf-8")
        total += len(data)
        if total > _MAX_DECODED_BYTES:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                f"project too large (max {_MAX_DECODED_BYTES} bytes uncompressed)",
            )
        decoded.append((f.path, data))
    return decoded


def _pack(files: list[ProjectFile]) -> bytes:
    """Decode + pack a file list to a stored archive, mapping errors to HTTP."""
    decoded = _decode_files(files)
    try:
        archive = pack_files(decoded)
    except ValueError as exc:  # bad path, duplicate, etc.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if len(archive) >= _MAX_ARCHIVE_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            "packed project exceeds storage limit",
        )
    return archive


@router.get("", response_model=list[ProjectMeta])
def list_my_projects(
    conn: Connection = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[ProjectMeta]:
    return list_projects_for_user(conn, user.id)


@router.post("", response_model=ProjectMeta, status_code=status.HTTP_201_CREATED)
def create(
    body: ProjectCreate,
    conn: Connection = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ProjectMeta:
    # Mirrors the projects_dev_code_matches_source CHECK, but as a clean 400.
    if body.source == "browser" and not body.files:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "browser projects require files")
    if body.source == "external_image" and body.files:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "external_image projects must not include files")

    archive = _pack(body.files) if body.files else None
    try:
        project_id = create_project(
            conn,
            user_id=user.id,
            name=body.name,
            language=body.language,
            source=body.source,
            dev_code_archive=archive,
        )
    except Exception as exc:  # UNIQUE (user_id, name), CHECK violations, etc.
        raise HTTPException(status.HTTP_409_CONFLICT, f"could not create project: {exc}") from exc

    meta = get_project_meta(conn, project_id)
    assert meta is not None
    return meta


@router.get("/{project_id}", response_model=ProjectMeta)
def get_meta(
    project_id: int,
    conn: Connection = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ProjectMeta:
    return _owned_meta(conn, project_id, user)


@router.put("/{project_id}/files", response_model=ProjectMeta)
def save_files(
    project_id: int,
    body: ProjectFiles,
    conn: Connection = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ProjectMeta:
    meta = _owned_meta(conn, project_id, user)
    if meta.source != "browser":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "only browser projects have editable code")
    if not body.files:
        # browser projects must keep a non-null dev_code_archive.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "files cannot be empty")
    archive = _pack(body.files)
    save_dev_code(conn, project_id, archive)
    refreshed = get_project_meta(conn, project_id)
    assert refreshed is not None
    return refreshed


@router.get("/{project_id}/files", response_model=ProjectFiles)
def get_files(
    project_id: int,
    conn: Connection = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ProjectFiles:
    """Return the project's dev code as a file structure for the editor.

    Files that aren't valid UTF-8 come back base64-encoded with encoding set
    accordingly, so the round-trip is lossless for binary assets too.
    """
    _owned_meta(conn, project_id, user)
    project: Project | None = get_project(conn, project_id)
    if project is None or project.dev_code_archive is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project has no dev code")

    files: list[ProjectFile] = []
    for path, data in unpack_files(project.dev_code_archive):
        try:
            files.append(ProjectFile(path=path, content=data.decode("utf-8"), encoding="utf-8"))
        except UnicodeDecodeError:
            files.append(
                ProjectFile(
                    path=path,
                    content=base64.b64encode(data).decode("ascii"),
                    encoding="base64",
                )
            )
    return ProjectFiles(files=files)


@router.get("/{project_id}/archive")
def download_dev_archive(
    project_id: int,
    conn: Connection = Depends(get_db),
    user: User = Depends(get_current_user),
) -> StreamingResponse:
    """Raw .tar.gz download of the dev code — for export/debugging. The editor
    uses GET /files instead."""
    _owned_meta(conn, project_id, user)
    project = get_project(conn, project_id)
    if project is None or project.dev_code_archive is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project has no dev code")
    return StreamingResponse(
        io.BytesIO(project.dev_code_archive),
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="project-{project_id}-dev.tar.gz"'},
    )


@router.post("/{project_id}/build", status_code=status.HTTP_202_ACCEPTED)
def build(
    project_id: int,
    conn: Connection = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    meta = _owned_meta(conn, project_id, user)
    if meta.source != "browser":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "only browser projects are built")
    job_id = enqueue_build_job(conn, project_id)
    job = get_build_job(conn, job_id)
    return {"build_job_id": job_id, "job": job}


@router.post("/{project_id}/submit", response_model=SubmitResult)
def submit(
    project_id: int,
    conn: Connection = Depends(get_db),
    user: User = Depends(get_current_user),
) -> SubmitResult:
    _owned_meta(conn, project_id, user)
    new_version = promote_to_submitted(conn, project_id)
    if new_version is None:
        # Normal outcome, not an error: dev not ready, or code changed since
        # the last test build. Tell the user to (re)test first.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "submit refused: test your latest changes first (no ready dev build, "
            "or code changed since the last build)",
        )
    return SubmitResult(submitted_version=new_version)