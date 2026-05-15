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

from services.common.common.types import BuildResult

log = logging.getLogger(__name__)


@dataclass
class LanguageManifest:
    name: str
    user_code_filename: str
    user_code_dest: str


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


def build_submission(
    language: str,
    user_code: bytes | Path,
    user_id: str,
    submission_id: str,
    *,
    base_image_version: str = "v1",
    registry_prefix: str = "snake",
    build_timeout_s: int = 60,
) -> BuildResult:
    start = time.monotonic()

    manifests = discover_languages()
    if language not in manifests:
        return BuildResult(
            success=False, image_tag=None, build_logs="",
            duration_s=time.monotonic() - start,
            error=f"unsupported language: {language} (available: {sorted(manifests)})",
        )
    manifest = manifests[language]

    client = docker.from_env()
    base_image = f"{registry_prefix}-base-{language}:{base_image_version}"
    submission_tag = f"{registry_prefix}-submission-{user_id}-{submission_id}:latest"

    try:
        client.images.get(base_image)
    except ImageNotFound:
        return BuildResult(
            success=False, image_tag=None, build_logs="",
            duration_s=time.monotonic() - start,
            error=f"base image not found: {base_image}",
        )

    if isinstance(user_code, Path):
        if not user_code.is_file():
            return BuildResult(
                success=False, image_tag=None, build_logs="",
                duration_s=time.monotonic() - start,
                error=f"user code file not found: {user_code}",
            )
        code_bytes = user_code.read_bytes()
    else:
        code_bytes = user_code

    build_dir = Path(tempfile.mkdtemp(prefix=f"build-{submission_id}-"))
    try:
        (build_dir / manifest.user_code_filename).write_bytes(code_bytes)
        (build_dir / "Dockerfile").write_text(
            f"FROM {base_image}\n"
            f"COPY {manifest.user_code_filename} {manifest.user_code_dest}\n"
        )

        log.info("building %s from %s", submission_tag, base_image)

        try:
            image, log_stream = client.images.build(
                path=str(build_dir),
                tag=submission_tag,
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

        return BuildResult(
            success=True,
            image_tag=submission_tag,
            build_logs=build_logs,
            duration_s=time.monotonic() - start,
        )

        #TODO: Store the build result.

    finally:
        shutil.rmtree(build_dir, ignore_errors=True)