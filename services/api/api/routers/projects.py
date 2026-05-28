# services/api/api/routers/projects.py
"""Project endpoints.

A project carries both the dev (iterative test) and submitted (ranked) state
on one row. The API never touches Docker:

  - submit -> a pure DB promotion (promote_to_submitted), which copies the
              current dev_image_tag onto the submitted side and bumps the
              version. Requires a ready dev build; the test runner builds
              automatically when a test match is enqueued.

Project code crosses the wire as a file structure (a list of {path, content,
encoding}), not a tarball. The API packs it into a .tar.gz for storage and
unpacks it back for the editor, so the browser never handles archive bytes.
"""
from __future__ import annotations

import base64
import binascii
import io
import re
import uuid

import docker
from docker.errors import DockerException
from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse
from psycopg import Connection



from sa_common.db.projects import (
    Project,
    ProjectMeta,
    create_project,
    delete_project,
    get_project,
    get_project_meta,
    list_projects_for_user,
    pack_files,
    project_name_exists,
    promote_to_submitted,
    record_dev_build_start,
    record_dev_build_success,
    restore_dev_from_submitted,
    save_dev_code,
    unpack_files,
    read_template_files,
)
from sa_common.db.test_match_jobs import get_bundle_keys_for_project
from sa_common.db.users import User

from api.auth import get_current_user
from api.bundler import get_bundler
from api.db import get_db
from api.schemas import (
    ProjectCreate,
    ProjectFile,
    ProjectFiles,
    SubmitResult,
)
from api.settings import get_settings, Settings

router = APIRouter(prefix="/projects", tags=["projects"])

_MAX_FILES = 2000
_MAX_DECODED_BYTES = 5 * 1024 * 1024   # total uncompressed payload
_MAX_ARCHIVE_BYTES = 5 * 1024 * 1024   # matches the projects_*_code_size CHECKs
_MAX_IMAGE_BYTES   = 500 * 1024 * 1024  # 500 MB cap for uploaded Docker tarballs
_REGISTRY_PREFIX   = "snake"


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
            status.HTTP_413_CONTENT_TOO_LARGE,
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
                status.HTTP_413_CONTENT_TOO_LARGE,
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
            status.HTTP_413_CONTENT_TOO_LARGE,
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
    settings: Settings = Depends(get_settings),
) -> ProjectMeta:
    if body.source == "external_image" and body.files:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "external_image projects must not include files")

    if body.files:
        archive = _pack(body.files)
    elif body.source == "browser":
        template = read_template_files(body.language, settings.templates_dir)
        if not template:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"no template available for language {body.language!r}",
            )
        archive = pack_files(template)
    else:
        archive = None
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


@router.get("/name-available")
def name_available(
    name: str,
    conn: Connection = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    """Live check for the new-project dialog. Names are globally unique."""
    trimmed = name.strip()
    if not trimmed:
        return {"available": False, "reason": "empty"}
    if project_name_exists(conn, trimmed):
        return {"available": False, "reason": "taken"}
    return {"available": True}


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


@router.get("/{project_id}/files/submitted", response_model=ProjectFiles)
def get_submitted_files(
    project_id: int,
    conn: Connection = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ProjectFiles:
    """Return the project's latest submitted code as a file structure (read-only view)."""
    _owned_meta(conn, project_id, user)
    project: Project | None = get_project(conn, project_id)
    if project is None or project.submitted_code_archive is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project has no submitted version")

    files: list[ProjectFile] = []
    for path, data in unpack_files(project.submitted_code_archive):
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


@router.post("/{project_id}/restore", response_model=ProjectMeta)
def restore(
    project_id: int,
    conn: Connection = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ProjectMeta:
    """Overwrite dev code with the latest submitted version."""
    meta = _owned_meta(conn, project_id, user)
    if meta.source != "browser":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "only browser projects have editable code")
    if not restore_dev_from_submitted(conn, project_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "project has no submitted version to restore from")
    refreshed = get_project_meta(conn, project_id)
    assert refreshed is not None
    return refreshed


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



@router.post("/{project_id}/upload-image", response_model=ProjectMeta)
async def upload_image(
    project_id: int,
    file: UploadFile,
    conn: Connection = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ProjectMeta:
    """Accept a docker-save tarball, load it, tag it as the dev image."""
    meta = _owned_meta(conn, project_id, user)
    if meta.source != "external_image":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "only external_image projects support image upload")

    size = file.size or 0
    if size > _MAX_IMAGE_BYTES:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"image too large (max {_MAX_IMAGE_BYTES // 1024 // 1024} MB)",
        )

    try:
        client = docker.from_env()
    except DockerException as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"docker unavailable: {e}")

    try:
        loaded = client.images.load(file.file)
    except DockerException as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"failed to load image: {e}")

    if not loaded:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no images found in tarball")

    image = loaded[0]
    safe_name = re.sub(r"[^a-z0-9._-]", "-", meta.name.lower()) or "unnamed"
    image_tag = f"{_REGISTRY_PREFIX}-{project_id}-{safe_name}-{uuid.uuid4().hex[:8]}"
    image.tag(image_tag)

    # GC any old dev images for this project (keep new tag + submitted tag).
    old_dev = meta.dev_image_tag
    if old_dev and old_dev != image_tag:
        prefix = f"{_REGISTRY_PREFIX}-{project_id}-"
        keep = {image_tag, meta.submitted_image_tag}
        try:
            for img in client.images.list():
                for t in img.tags:
                    if t.rsplit(":", 1)[0].startswith(prefix) and t.rsplit(":", 1)[0] not in keep:
                        try:
                            client.images.remove(t, force=True)
                        except DockerException:
                            pass
        except DockerException:
            pass

    record_dev_build_start(conn, project_id)
    record_dev_build_success(conn, project_id, image_tag)

    refreshed = get_project_meta(conn, project_id)
    assert refreshed is not None
    return refreshed


@router.post("/{project_id}/submit", response_model=SubmitResult)
def submit(
    project_id: int,
    conn: Connection = Depends(get_db),
    user: User = Depends(get_current_user),
) -> SubmitResult:
    meta = _owned_meta(conn, project_id, user)
    new_version = promote_to_submitted(conn, project_id)
    if new_version is None:
        # Normal outcome, not an error. Tailor the message to why it's blocked:
        # a crashed build needs fixing; otherwise the dev image is missing or
        # stale (code changed since the last test run).
        if meta.dev_build_status == "crashed":
            detail = "Your agent crashed before it could play. Fix it and run a test before submitting."
        else:
            detail = "Test your project before submitting."
        raise HTTPException(status.HTTP_409_CONFLICT, detail)
    return SubmitResult(submitted_version=new_version)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete(
    project_id: int,
    conn: Connection = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    _owned_meta(conn, project_id, user)  # 404 if not found or not owned
    bundle_keys = get_bundle_keys_for_project(conn, project_id)
    delete_project(conn, project_id)
    bundler = get_bundler()
    for key in bundle_keys:
        try:
            bundler.delete(key)
        except Exception:
            pass  # storage cleanup is best-effort; project row is already gone