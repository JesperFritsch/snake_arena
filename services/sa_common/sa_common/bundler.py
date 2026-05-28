# services/sa_common/sa_common/bundler.py
"""Storage abstraction for match bundles (replay + logs + analysis zip).

A bundle is addressed by an opaque `key` (e.g. "test-matches/42/bundle.zip").
The orchestrator calls put() to store it; the API calls url() to hand the
browser a fetchable location. Swapping disk for R2 is a matter of swapping the
implementation — nothing else knows where bundles physically live.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol


class IBundler(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def url(self, key: str) -> str: ...
    def delete(self, key: str) -> None: ...


class DiskBundler:
    """Stores bundles under a local directory served by a static file host
    (nginx in dev). `base_dir` is where bytes are written (the orchestrator's
    side); `public_base_url` is where the browser fetches them (the API's side).
    A given process only needs the half it uses.
    """

    def __init__(self, base_dir: str | Path, public_base_url: str | None = None):
        self._base_dir = Path(base_dir)
        self._public_base_url = public_base_url.rstrip("/") if public_base_url else None

    def put(self, key: str, data: bytes) -> None:
        path = self._safe_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def url(self, key: str) -> str:
        if not self._public_base_url:
            raise RuntimeError("DiskBundler has no public_base_url (set REPLAY_HOST)")
        return f"{self._public_base_url}/{key.lstrip('/')}"

    def delete(self, key: str) -> None:
        try:
            self._safe_path(key).unlink()
        except FileNotFoundError:
            pass

    def _safe_path(self, key: str) -> Path:
        # Guard against path traversal escaping base_dir via a crafted key.
        base = self._base_dir.resolve()
        path = (base / key).resolve()
        if path != base and base not in path.parents:
            raise ValueError(f"unsafe bundle key: {key!r}")
        return path


def bundler_from_env() -> IBundler:
    """Build the configured bundler. BUNDLER_BACKEND selects the implementation
    (default "disk"). Each service supplies the env it needs: the orchestrator
    BUNDLE_DIR (where to write), the API REPLAY_HOST (where the browser reads)."""
    backend = os.environ.get("BUNDLER_BACKEND", "disk")
    if backend == "disk":
        base_dir = os.environ.get("BUNDLE_DIR", "./sim-artifacts")
        return DiskBundler(base_dir=base_dir, public_base_url=os.environ.get("REPLAY_HOST"))
    raise ValueError(f"unknown BUNDLER_BACKEND: {backend!r}")
