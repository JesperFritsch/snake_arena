import logging
import os
import shutil
import tempfile
import time
import tomllib
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import docker
from docker.errors import BuildError, ImageNotFound

from sa_common.types import BuildResult
from sa_common.db.connection import get_conn
from sa_common.db.projects import (
    get_project,
    Project,
    record_dev_build_start,
    record_dev_build_success,
    record_dev_build_failure,
    get_project_meta,
    promote_to_submitted,
    unpack_archive,
)

log = logging.getLogger(__name__)


@dataclass
class LanguageManifest:
    name: str
    user_code_dest: str
    user_entry_file: str


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
    base_image_version: str = "v1",
    registry_prefix: str = "snake",
    build_timeout_s: int = 60,
) -> BuildResult:
    start = time.monotonic()
    with get_conn() as conn:
        project: Project = get_project(conn, project_id=project_id)
        if project is None:
            return BuildResult(
                success=False, 
                build_logs="", 
                duration_s=time.monotonic() - start, 
                error="project not found"
            )
        if project.dev_code_archive is None:
            return BuildResult(
                success=False,
                build_logs="", 
                duration_s=time.monotonic() - start, 
                error="project has no dev code"
            )
        record_dev_build_start(conn, project_id)
    language = project.language
    user_id = project.user_id
    manifests = discover_languages()
    if language not in manifests:
        return BuildResult(
            success=False, image_tag=None, build_logs="",
            duration_s=time.monotonic() - start,
            error=f"unsupported language: {language} (available: {sorted(manifests)})",
        )
    manifest = manifests[language]

    client = docker.from_env()
    safe_name = "".join(
        c if c.isalnum() or c in "-_." else "-"
        for c in project.name.lower()
    )
    base_image = f"{registry_prefix}-base-{language}:{base_image_version}"
    image_tag = f"{registry_prefix}-{user_id}-{safe_name}:latest"

    try:
        client.images.get(base_image)
    except ImageNotFound:
        return BuildResult(
            success=False, image_tag=None, build_logs="",
            duration_s=time.monotonic() - start,
            error=f"base image not found: {base_image}",
        )
    
    build_dir = Path(tempfile.mkdtemp(prefix=f"build-{user_id}-{safe_name}"))

    unpack_archive(project.dev_code_archive, build_dir)

    if not (build_dir / manifest.user_entry_file).is_file():
        return BuildResult(
            success=False, image_tag=None, build_logs="",
            duration_s=time.monotonic() - start,
            error=f"{manifest.user_entry_file} file not found in project '{project.name}'",
        )

    try:
        (build_dir / "Dockerfile").write_text(
            f"FROM {base_image}\n"
            f"COPY . {manifest.user_code_dest}\n"
        )

        (build_dir / ".dockerignore").write_text(
            "Dockerfile\n"
            ".dockerignore\n"
        )


        log.info("building %s from %s", image_tag, base_image)

        try:
            image, log_stream = client.images.build(
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
            return BuildResult(
                success=False, image_tag=None, build_logs=build_logs,
                duration_s=time.monotonic() - start,
                error=f"build failed: {e.msg}",
            )

        build_logs = "\n".join(
            str(entry.get("stream", "")).rstrip()
            for entry in log_stream
            if entry.get("stream")
        )

        result = BuildResult(
            success=True,
            image_tag=image_tag,
            build_logs=build_logs,
            duration_s=time.monotonic() - start,
        )

        with get_conn(autocommit=True) as conn:
            if result.success:
                record_dev_build_success(conn, project_id, image_tag)
            else:
                record_dev_build_failure(conn, project_id)

        return result


    finally:
        shutil.rmtree(build_dir, ignore_errors=True)
