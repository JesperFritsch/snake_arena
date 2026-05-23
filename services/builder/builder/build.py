import logging
import os
import shutil
import tempfile
import time
import tomllib
import uuid
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import docker
from docker.errors import BuildError, DockerException, ImageNotFound

from sa_common.types import BuildResult
from sa_common.db.connection import get_conn
from sa_common.db.projects import (
    Project,
    get_project,
    record_dev_build_failure,
    record_dev_build_start,
    record_dev_build_success,
    unpack_archive,
)

log = logging.getLogger(__name__)


@dataclass
class LanguageManifest:
    name: str
    user_code_dest: str
    user_entry_file: str
    build_cmd: str | None = None


def _sandbox_images_dir() -> Path:
    return Path(os.environ.get("SANDBOX_IMAGES_DIR", "sandbox-images")).resolve()


@cache
def discover_languages() -> dict[str, LanguageManifest]:
    base = _sandbox_images_dir()
    if not base.is_dir():
        log.warning("sandbox images dir not found: %s", base)
        return {}

    languages: dict[str, LanguageManifest] = {}
    for manifest_path in base.glob("*/manifest.toml"):
        try:
            with open(manifest_path, "rb") as f:
                data = tomllib.load(f)["language"]
            manifest = LanguageManifest(**data)
            languages[manifest.name] = manifest
        except Exception as e:
            log.warning("failed to load %s: %s", manifest_path, e)
    return languages


def build_project(
    project_id: int,
    registry_prefix: str = "snake",
    build_timeout_s: int = 60,
) -> BuildResult:
    """Build a project's dev image.

    Invariant: as long as the project exists, dev_build_status is updated
    to 'ready' or 'failed' before this function returns — never stuck at
    'building'.
    """
    start = time.monotonic()

    # Project lookup — the only error path that doesn't touch project state.
    with get_conn(autocommit=True) as conn:
        project: Project | None = get_project(conn, project_id=project_id)
    if project is None:
        return BuildResult(
            success=False,
            duration_s=time.monotonic() - start,
            error="project not found",
        )

    # From here on we own the project's state. Every return path below
    # passes through the record_dev_build_* call at the bottom.
    with get_conn(autocommit=True) as conn:
        record_dev_build_start(conn, project_id)

    try:
        result = _run_build(
            project=project,
            start=start,
            registry_prefix=registry_prefix,
            build_timeout_s=build_timeout_s,
        )
    except Exception as e:
        log.exception("unexpected error building project %d", project_id)
        result = BuildResult(
            success=False,
            duration_s=time.monotonic() - start,
            error=f"internal error: {e}",
        )

    with get_conn(autocommit=True) as conn:
        if result.success:
            record_dev_build_success(conn, project_id, result.image_tag)
            _gc_project_images(
                registry_prefix=registry_prefix,
                project_id=project_id,
                keep={result.image_tag, project.submitted_image_tag},
            )
        else:
            record_dev_build_failure(conn, project_id)

    return result


def _gc_project_images(
    registry_prefix: str, project_id: int, keep: set[str | None]
) -> None:
    """Reap a project's stale images after a successful build.

    Removes every image tagged for this project except the ones in `keep`
    (the current dev build and the current submitted image). This reclaims
    superseded dev builds and old submitted images orphaned by a re-submit.
    Matching is by the project-unique prefix `{registry_prefix}-{project_id}-`,
    so sibling projects (and base images) are never touched.
    """
    prefix = f"{registry_prefix}-{project_id}-"
    keep_repos = {t for t in keep if t}
    try:
        client = docker.from_env()
        images = client.images.list()
    except DockerException as e:
        log.warning("image gc: docker unavailable: %s", e)
        return
    for img in images:
        for tag in img.tags:                    # e.g. "snake-5-bot-ab12cd34:latest"
            repo = tag.rsplit(":", 1)[0]        # strip the ":latest"
            if repo.startswith(prefix) and repo not in keep_repos:
                try:
                    client.images.remove(tag, force=True)
                    log.info("image gc removed %s", tag)
                except DockerException as e:
                    log.warning("image gc could not remove %s: %s", tag, e)
                break  # this image is gone; move to the next one


def _run_build(
    project: Project,
    start: float,
    registry_prefix: str,
    build_timeout_s: int,
) -> BuildResult:
    """Does the actual build. Returns a BuildResult for expected failures;
    unexpected exceptions propagate to build_project's catch-all."""

    def _fail(error: str, build_logs: str = "") -> BuildResult:
        return BuildResult(
            success=False,
            build_logs=build_logs,
            duration_s=time.monotonic() - start,
            error=error,
        )

    if project.dev_code_archive is None:
        return _fail("project has no dev code")

    manifests = discover_languages()
    if project.language not in manifests:
        return _fail(
            f"unsupported language: {project.language} "
            f"(available: {sorted(manifests)})"
        )
    manifest = manifests[project.language]

    safe_name = "".join(
        c if c.isalnum() or c in "-_." else "-"
        for c in project.name.lower()
    ) or "unnamed"
    base_image = f"{registry_prefix}-base-{project.language}"
    # project.id (not user_id) keys the tag so a project's images form a unique
    # group that _gc_project_images can safely reap without touching siblings.
    image_tag = f"{registry_prefix}-{project.id}-{safe_name}-{uuid.uuid4().hex[:8]}"

    try:
        client = docker.from_env()
    except DockerException as e:
        return _fail(f"docker daemon unavailable: {e}")

    try:
        client.images.get(base_image)
    except ImageNotFound:
        return _fail(f"base image not found: {base_image}")
    except DockerException as e:
        return _fail(f"docker error fetching base image: {e}")

    build_dir = Path(tempfile.mkdtemp(prefix=f"build-{project.user_id}-{safe_name}-"))
    try:
        try:
            unpack_archive(project.dev_code_archive, build_dir)
        except Exception as e:
            return _fail(f"failed to unpack code archive: {e}")

        if not (build_dir / manifest.user_entry_file).is_file():
            return _fail(
                f"{manifest.user_entry_file} not found in project '{project.name}'"
            )

        dockerfile = f"FROM {base_image}\nCOPY --chown=1000:1000 . {manifest.user_code_dest}\n"
        if manifest.build_cmd:
            dockerfile += f"RUN {manifest.build_cmd}\n"
        (build_dir / "Dockerfile").write_text(dockerfile)
        (build_dir / ".dockerignore").write_text("Dockerfile\n.dockerignore\n")

        log.info("building %s from %s", image_tag, base_image)
        try:
            _, log_stream = client.images.build(
                path=str(build_dir),
                tag=image_tag,
                rm=True,
                forcerm=True,
                pull=False,
                timeout=build_timeout_s,
            )
        except BuildError as e:
            build_logs = "\n".join(
                str(line.get("stream", "")) for line in e.build_log
            )
            return _fail(f"build failed: {e.msg}", build_logs=build_logs)
        except DockerException as e:
            return _fail(f"docker error during build: {e}")

        build_logs = "\n".join(
            str(entry.get("stream", "")).rstrip()
            for entry in log_stream
            if entry.get("stream")
        )

        return BuildResult(
            success=True,
            image_tag=image_tag,
            build_logs=build_logs,
            duration_s=time.monotonic() - start,
        )

    finally:
        shutil.rmtree(build_dir, ignore_errors=True)