"""Unauthenticated download endpoints for reference material.

Serves sim_interface.proto and per-language harness zips so users building
custom Docker images have a concrete starting point.
"""
from __future__ import annotations

import io
import tomllib
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse, StreamingResponse

from sa_common.db.users import User
from api.auth import get_current_user
from api.settings import Settings, get_settings

router = APIRouter(prefix="/download", tags=["download"])


def _name_to_dir(sandbox_dir: Path) -> dict[str, Path]:
    """Map manifest `name` → sandbox image directory (e.g. 'javascript' → js/)."""
    result: dict[str, Path] = {}
    for manifest_path in sandbox_dir.glob("*/manifest.toml"):
        try:
            with open(manifest_path, "rb") as f:
                name = tomllib.load(f).get("language", {}).get("name")
            if name:
                result[name] = manifest_path.parent
        except Exception:
            pass
    return result


@router.get("/proto")
def get_proto(
    settings: Settings = Depends(get_settings),
    _: User = Depends(get_current_user),
) -> FileResponse:
    proto = settings.sandbox_images_dir / "proto" / "sim_interface.proto"
    if not proto.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "proto file not found")
    return FileResponse(proto, filename="sim_interface.proto", media_type="text/plain")


@router.get("/harness/{language}")
def get_harness(
    language: str,
    settings: Settings = Depends(get_settings),
    _: User = Depends(get_current_user),
) -> StreamingResponse:
    name_map = _name_to_dir(settings.sandbox_images_dir)
    lang_dir = name_map.get(language)
    if lang_dir is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no harness for language {language!r}")

    harness_dir = lang_dir / "harness"
    if not harness_dir.is_dir():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"harness directory missing for {language!r}")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        proto = settings.sandbox_images_dir / "proto" / "sim_interface.proto"
        dockerfile = harness_dir.parent / "Dockerfile"
        if proto.is_file():
            zf.write(proto, "proto/sim_interface.proto")
        if dockerfile.is_file():
            zf.write(dockerfile, "Dockerfile")

        for f in sorted(harness_dir.rglob("*")):
            if not f.is_file():
                continue
            # Skip compiled artefacts that aren't useful as reference.
            rel = f.relative_to(harness_dir)
            parts = rel.parts
            if any(p in ("dist", "target", "__pycache__", "node_modules") for p in parts):
                continue
            if f.suffix in (".pyc", ".class", ".o"):
                continue
            zf.write(f, f"harness/{rel}")

    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="snake-harness-{language}.zip"'},
    )
