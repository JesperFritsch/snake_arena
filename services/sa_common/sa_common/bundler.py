# services/sa_common/sa_common/bundler.py
"""Storage abstraction for match bundles (replay + logs + analysis zip).

A bundle is addressed by an opaque `key` (e.g. "matches/abc/bundle.zip").
The orchestrator calls put() to store it; the API calls url() to hand the
browser a fetchable location. Swapping disk for HTTP or R2 is a matter of
swapping the implementation — nothing else knows where bundles physically live.

Backends (BUNDLER_BACKEND env var):
  disk  — DiskBundler: writes to a local directory (requires a shared volume
          between the writer and the file-server). Default.
  http  — HttpBundler: PUT/GET/DELETE against a file-server that speaks
          WebDAV (nginx ngx_http_dav_module). No shared volume needed.
"""
from __future__ import annotations

import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Protocol


class IBundler(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def url(self, key: str) -> str: ...
    def delete(self, key: str) -> None: ...


# --------------------------------------------------------------------------
# Disk backend (dev fallback, requires a shared volume mount)
# --------------------------------------------------------------------------

class DiskBundler:
    """Stores bundles under a local directory served by a static file host.

    Only useful when the writer (orchestrator) and reader (file-server) share
    a filesystem. Prefer HttpBundler for containerised deployments.
    """

    def __init__(self, base_dir: str | Path, public_base_url: str | None = None):
        self._base_dir = Path(base_dir)
        self._public_base_url = public_base_url.rstrip("/") if public_base_url else None

    def put(self, key: str, data: bytes) -> None:
        path = self._safe_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, key: str) -> bytes:
        return self._safe_path(key).read_bytes()

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
        base = self._base_dir.resolve()
        path = (base / key).resolve()
        if path != base and base not in path.parents:
            raise ValueError(f"unsafe bundle key: {key!r}")
        return path


# --------------------------------------------------------------------------
# HTTP backend (nginx WebDAV or any S3-compatible upload endpoint)
# --------------------------------------------------------------------------

class HttpBundler:
    """PUT/DELETE bundles via WebDAV; GET bundles from the static-serve root.

    upload_url  — WebDAV base URL for PUT/DELETE
                  (e.g. http://file-server/upload).
    read_url    — static-serve base URL for GET
                  (e.g. http://file-server). Defaults to upload_url if omitted.
    public_base_url — browser-accessible base URL returned by url()
                  (e.g. http://localhost:8081). Required by the API.
    """

    def __init__(
        self,
        upload_url: str,
        read_url: str | None = None,
        public_base_url: str | None = None,
    ):
        self._upload_url = upload_url.rstrip("/")
        self._read_url = (read_url or upload_url).rstrip("/")
        self._public_base_url = public_base_url.rstrip("/") if public_base_url else None

    def put(self, key: str, data: bytes) -> None:
        url = f"{self._upload_url}/{key.lstrip('/')}"
        req = urllib.request.Request(url, data=data, method="PUT")
        with urllib.request.urlopen(req) as resp:
            resp.read()

    def get(self, key: str) -> bytes:
        url = f"{self._read_url}/{key.lstrip('/')}"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req) as resp:
            return resp.read()

    def url(self, key: str) -> str:
        if not self._public_base_url:
            raise RuntimeError("HttpBundler has no public_base_url (set REPLAY_HOST)")
        return f"{self._public_base_url}/{key.lstrip('/')}"

    def delete(self, key: str) -> None:
        url = f"{self._upload_url}/{key.lstrip('/')}"
        req = urllib.request.Request(url, method="DELETE")
        try:
            with urllib.request.urlopen(req) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise


# --------------------------------------------------------------------------
# Factory
# --------------------------------------------------------------------------

def bundler_from_env() -> IBundler:
    """Build the configured bundler from environment variables.

    BUNDLER_BACKEND  disk | http  (default: disk)

    disk:
      BUNDLE_DIR      local path to write bundles (default: ./sim-artifacts)
      REPLAY_HOST     public base URL for url() — required by the API

    http:
      BUNDLE_UPLOAD_URL  WebDAV base URL for PUT/DELETE
                         (e.g. http://file-server/upload)
      BUNDLE_READ_URL    static-serve base URL for GET
                         (e.g. http://file-server). Defaults to BUNDLE_UPLOAD_URL.
      REPLAY_HOST        public base URL for url() — required by the API
    """
    backend = os.environ.get("BUNDLER_BACKEND", "disk")
    if backend == "disk":
        base_dir = os.environ.get("BUNDLE_DIR", "./sim-artifacts")
        return DiskBundler(base_dir=base_dir, public_base_url=os.environ.get("REPLAY_HOST"))
    if backend == "http":
        upload_url = os.environ.get("BUNDLE_UPLOAD_URL")
        if not upload_url:
            raise ValueError("BUNDLE_UPLOAD_URL is required when BUNDLER_BACKEND=http")
        return HttpBundler(
            upload_url=upload_url,
            read_url=os.environ.get("BUNDLE_READ_URL"),
            public_base_url=os.environ.get("REPLAY_HOST"),
        )
    raise ValueError(f"unknown BUNDLER_BACKEND: {backend!r}")
