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

import asyncio
import base64
import binascii
import io
import json
import re
import shutil
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import docker
from docker.errors import DockerException
from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse
from psycopg import Connection



from sa_common.db.projects import (
    Project,
    ProjectMeta,
    count_projects_for_user,
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
from sa_common.db.matches import prune_obsolete_ranked_matches
from sa_common.db.test_match_jobs import get_bundle_keys_for_project
from sa_common.db.users import User

from api.auth import get_current_user
from api.bundler import get_bundler
from api.db import get_db
from api.rate_limit import (
    RateLimitResult,
    check_submit_quotas_available,
    check_upload_quota_available,
    consume_submit_quotas,
    consume_upload_quota,
    peek_submit_quotas,
    peek_upload_quota,
)
from api.schemas import (
    ProjectCreate,
    ProjectFile,
    ProjectFiles,
    QuotaStatus,
    SubmitQuotaStatus,
    SubmitResult,
    UploadImageStart,
)
from api.settings import get_settings, Settings

router = APIRouter(prefix="/projects", tags=["projects"])

_MAX_FILES = 2000
_MAX_DECODED_BYTES = 5 * 1024 * 1024   # total uncompressed payload
_MAX_ARCHIVE_BYTES = 5 * 1024 * 1024   # matches the projects_*_code_size CHECKs
_MAX_IMAGE_BYTES   = 500 * 1024 * 1024  # 500 MB cap for uploaded Docker tarballs
_REGISTRY_PREFIX   = "snake"

# Chunked image upload
_UPLOAD_TMP_DIR  = Path(tempfile.gettempdir()) / "snake-uploads"
_MAX_CHUNK_BYTES = 90 * 1024 * 1024   # 90 MB per chunk — stays under Cloudflare's 100 MB limit
_MAX_CHUNKS      = 10                  # 10 × 90 MB = 900 MB, gated by _MAX_IMAGE_BYTES anyway
_UPLOAD_TTL      = timedelta(hours=2)
_UPLOAD_ID_RE    = re.compile(r"^[0-9a-f]{32}$")


class _ChunkReader(io.RawIOBase):
    """Streams assembled chunk files to the Docker SDK without loading all at once."""

    def __init__(self, upload_dir: Path, total_chunks: int) -> None:
        self._chunks = [upload_dir / f"{i}.chunk" for i in range(total_chunks)]
        self._ci = 0
        self._buf = b""
        self._pos = 0

    def readable(self) -> bool:
        return True

    def readinto(self, b: bytearray) -> int:
        while self._pos >= len(self._buf):
            if self._ci >= len(self._chunks):
                return 0
            self._buf = self._chunks[self._ci].read_bytes()
            self._pos = 0
            self._ci += 1
        take = min(len(b), len(self._buf) - self._pos)
        b[:take] = self._buf[self._pos : self._pos + take]
        self._pos += take
        return take


def _resolve_upload_dir(upload_id: str) -> Path:
    if not _UPLOAD_ID_RE.fullmatch(upload_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid upload_id")
    return _UPLOAD_TMP_DIR / upload_id


def _read_upload_meta(upload_id: str, project_id: int, user_id: int) -> dict:
    d = _resolve_upload_dir(upload_id)
    meta_path = d / "meta.json"
    if not meta_path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "upload session not found or expired")
    meta = json.loads(meta_path.read_text())
    if meta["project_id"] != project_id or meta["user_id"] != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "upload session not found or expired")
    return meta


async def stale_upload_cleanup_task() -> None:
    """Background task: deletes upload dirs that were never finalized."""
    while True:
        await asyncio.sleep(3600)
        if not _UPLOAD_TMP_DIR.is_dir():
            continue
        cutoff = datetime.now(tz=timezone.utc) - _UPLOAD_TTL
        for d in _UPLOAD_TMP_DIR.iterdir():
            if not d.is_dir():
                continue
            meta_path = d / "meta.json"
            try:
                if meta_path.exists():
                    created = datetime.fromisoformat(
                        json.loads(meta_path.read_text())["created_at"]
                    )
                    if created < cutoff:
                        shutil.rmtree(d, ignore_errors=True)
                else:
                    shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass


def _owned_meta(conn: Connection, project_id: int, user: User) -> ProjectMeta:
    meta = get_project_meta(conn, project_id)
    if meta is None or meta.user_id != user.id:
        # 404 (not 403) so we don't leak which project ids exist.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")
    return meta


def _rl_to_quota(result: RateLimitResult) -> QuotaStatus:
    return QuotaStatus(
        limit=result.limit,
        used=result.used,
        remaining=result.remaining,
        next_slot_at=result.reset_at if result.used > 0 else None,
        window_seconds=result.window_seconds,
    )


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
    MAX_PROJECTS = 5
    if count_projects_for_user(conn, user.id) >= MAX_PROJECTS:
        raise HTTPException(status.HTTP_409_CONFLICT, f"project limit reached ({MAX_PROJECTS} max)")

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


@router.get("/submit-quota", response_model=SubmitQuotaStatus)
async def get_submit_quota(
    user: User = Depends(get_current_user),
) -> SubmitQuotaStatus:
    """Hourly + daily ranked-submission quota for the caller. Read-only."""
    hourly, daily = await peek_submit_quotas(user.id)
    return SubmitQuotaStatus(hourly=_rl_to_quota(hourly), daily=_rl_to_quota(daily))


@router.get("/upload-image-quota", response_model=QuotaStatus)
async def get_upload_image_quota(
    user: User = Depends(get_current_user),
) -> QuotaStatus:
    """Daily image-upload quota for the caller. Read-only."""
    return _rl_to_quota(await peek_upload_quota(user.id))


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



@router.post("/{project_id}/upload-image/start")
async def upload_image_start(
    project_id: int,
    body: UploadImageStart,
    conn: Connection = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Begin a chunked image upload. Returns an upload_id for subsequent chunk/finalize calls."""
    await check_upload_quota_available(user.id)

    meta = _owned_meta(conn, project_id, user)
    if meta.source != "external_image":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "only external_image projects support image upload")
    if body.total_chunks > _MAX_CHUNKS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"too many chunks (max {_MAX_CHUNKS})")

    upload_id = uuid.uuid4().hex
    upload_dir = _UPLOAD_TMP_DIR / upload_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    (upload_dir / "meta.json").write_text(json.dumps({
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
        "total_chunks": body.total_chunks,
        "project_id": project_id,
        "user_id": user.id,
    }))
    return {"upload_id": upload_id}


@router.post("/{project_id}/upload-image/chunk")
async def upload_image_chunk(
    project_id: int,
    upload_id: str,
    index: int,
    file: UploadFile,
    user: User = Depends(get_current_user),
) -> dict:
    """Upload one chunk of a previously started image upload."""
    meta = _read_upload_meta(upload_id, project_id, user.id)
    total_chunks = meta["total_chunks"]
    if index < 0 or index >= total_chunks:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"chunk index out of range (0–{total_chunks - 1})")
    if file.size is None:
        raise HTTPException(status.HTTP_411_LENGTH_REQUIRED, "Content-Length required")
    if file.size > _MAX_CHUNK_BYTES:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"chunk too large (max {_MAX_CHUNK_BYTES // 1024 // 1024} MB)",
        )
    upload_dir = _resolve_upload_dir(upload_id)
    (upload_dir / f"{index}.chunk").write_bytes(await file.read())
    return {"received": index}


@router.post("/{project_id}/upload-image/finalize", response_model=ProjectMeta)
async def upload_image_finalize(
    project_id: int,
    upload_id: str,
    conn: Connection = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ProjectMeta:
    """Assemble all chunks, load the Docker image, and clean up the temp dir."""
    await check_upload_quota_available(user.id)

    meta_json = _read_upload_meta(upload_id, project_id, user.id)
    total_chunks = meta_json["total_chunks"]
    upload_dir = _resolve_upload_dir(upload_id)

    try:
        for i in range(total_chunks):
            if not (upload_dir / f"{i}.chunk").exists():
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"chunk {i} missing")

        total_size = sum((upload_dir / f"{i}.chunk").stat().st_size for i in range(total_chunks))
        if total_size > _MAX_IMAGE_BYTES:
            raise HTTPException(
                status.HTTP_413_CONTENT_TOO_LARGE,
                f"image too large (max {_MAX_IMAGE_BYTES // 1024 // 1024} MB)",
            )

        try:
            client = docker.from_env()
        except DockerException as e:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"docker unavailable: {e}")

        reader = io.BufferedReader(_ChunkReader(upload_dir, total_chunks))
        try:
            loaded = client.images.load(reader)
        except DockerException as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"failed to load image: {e}")
    finally:
        shutil.rmtree(upload_dir, ignore_errors=True)

    if not loaded:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no images found in tarball")

    meta = _owned_meta(conn, project_id, user)
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

    # Consume the daily quota slot only after the upload succeeds.
    await consume_upload_quota(user.id)

    refreshed = get_project_meta(conn, project_id)
    assert refreshed is not None
    return refreshed


@router.post("/{project_id}/submit", response_model=SubmitResult)
async def submit(
    project_id: int,
    conn: Connection = Depends(get_db),
    user: User = Depends(get_current_user),
) -> SubmitResult:
    await check_submit_quotas_available(user.id)

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

    # Consume both windows only after a successful promotion so a 409 ("test
    # first") doesn't cost the user a slot.
    await consume_submit_quotas(user.id)

    # Bumping submitted_version may orphan ranked matches where every
    # participant's project_version is now stale — those contribute to no
    # leaderboard anymore. Prune them and purge their bundles.
    bundler = get_bundler()
    for key in prune_obsolete_ranked_matches(conn):
        try:
            bundler.delete(key)
        except Exception:
            pass  # storage cleanup is best-effort; DB rows are already gone

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
    # Deleting the project cascades to match_participants — any ranked match
    # whose only remaining participants are stale (or none) is now obsolete.
    bundle_keys.extend(prune_obsolete_ranked_matches(conn))
    bundler = get_bundler()
    for key in bundle_keys:
        try:
            bundler.delete(key)
        except Exception:
            pass  # storage cleanup is best-effort; project row is already gone