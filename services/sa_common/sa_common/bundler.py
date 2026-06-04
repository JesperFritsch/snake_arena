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
        path = self._safe_path(key)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        # Walk parents up to base_dir, dropping each one while it's empty so
        # we don't leak per-match directories (e.g. test-matches/24/).
        base = self._base_dir.resolve()
        parent = path.parent
        while parent != base and base in parent.parents:
            try:
                parent.rmdir()
            except OSError:
                break  # non-empty (sibling exists) or already gone
            parent = parent.parent

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

    upload_url       — WebDAV base URL for PUT/DELETE (e.g. http://file-server/upload).
    read_url         — static-serve base URL for GET (e.g. http://file-server).
    public_base_url  — browser-accessible base URL returned by url()
                       (e.g. http://localhost:8081). None disables url().
    """

    def __init__(
        self,
        upload_url: str,
        read_url: str,
        public_base_url: str | None,
    ):
        self._upload_url = upload_url.rstrip("/")
        self._read_url = read_url.rstrip("/")
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
            return  # nothing to clean up
        # Best-effort: drop the immediate parent collection. Our key
        # convention puts one bundle per id-dir (e.g. test-matches/24/
        # bundle.zip), so the dir is empty by construction now. nginx
        # WebDAV requires Depth: infinity for collection DELETE — fine
        # here because we know the collection has no other contents.
        # We do NOT walk further up: matches/ and test-matches/ contain
        # sibling id-dirs, and a recursive delete there would wipe them.
        if "/" not in key:
            return
        parent_key = key.rsplit("/", 1)[0]
        if "/" not in parent_key:
            return  # only one segment above (e.g. matches/) — leave it alone
        parent_url = f"{self._upload_url}/{parent_key.lstrip('/')}/"
        parent_req = urllib.request.Request(
            parent_url, method="DELETE", headers={"Depth": "infinity"},
        )
        try:
            with urllib.request.urlopen(parent_req) as resp:
                resp.read()
        except urllib.error.HTTPError:
            pass  # 404 / 405 / 409 — best-effort cleanup


# --------------------------------------------------------------------------
# R2 backend (Cloudflare R2 / S3-compatible object storage)
# --------------------------------------------------------------------------

class R2Bundler:
    """Stores bundles in a Cloudflare R2 bucket via the S3-compatible API.

    endpoint_url     — https://<account_id>.r2.cloudflarestorage.com
    access_key_id    — R2 API token access key
    secret_access_key— R2 API token secret key
    bucket           — R2 bucket name
    public_base_url  — browser-accessible base URL returned by url()
                       (the bucket's public URL or custom domain)
    """

    def __init__(
        self,
        endpoint_url: str,
        access_key_id: str,
        secret_access_key: str,
        bucket: str,
        public_base_url: str | None,
    ):
        import boto3
        self._bucket = bucket
        self._public_base_url = public_base_url.rstrip("/") if public_base_url else None
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name="auto",
        )

    def put(self, key: str, data: bytes) -> None:
        self._client.put_object(Bucket=self._bucket, Key=key.lstrip("/"), Body=data)

    def get(self, key: str) -> bytes:
        resp = self._client.get_object(Bucket=self._bucket, Key=key.lstrip("/"))
        return resp["Body"].read()

    def url(self, key: str) -> str:
        if not self._public_base_url:
            raise RuntimeError("R2Bundler has no public_base_url (set REPLAY_HOST)")
        return f"{self._public_base_url}/{key.lstrip('/')}"

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self._bucket, Key=key.lstrip("/"))


# --------------------------------------------------------------------------
# Factory
# --------------------------------------------------------------------------

def bundler_from_env() -> IBundler:
    """Build the configured bundler from environment variables.

    BUNDLER_BACKEND    disk | http | r2  — required.
    REPLAY_HOST        public base URL for url(). Required by the API; for
                       orchestrator daemons that only put/get it can be empty.

    disk backend:
      BUNDLE_DIR         local path to write bundles
    http backend:
      BUNDLE_UPLOAD_URL  WebDAV base URL for PUT/DELETE
                         (e.g. http://file-server/upload)
      BUNDLE_READ_URL    static-serve base URL for GET
                         (e.g. http://file-server)
    r2 backend:
      R2_ENDPOINT_URL      https://<account_id>.r2.cloudflarestorage.com
      R2_ACCESS_KEY_ID     R2 API token access key
      R2_SECRET_ACCESS_KEY R2 API token secret key
      R2_BUCKET            bucket name
    """
    backend = os.environ["BUNDLER_BACKEND"]
    # REPLAY_HOST is required for the API; orchestrator daemons (which only
    # PUT/GET internally) set it to an empty string in compose. Treat empty
    # as "no public URL" so .url() raises if called.
    replay_host = os.environ["REPLAY_HOST"] or None
    if backend == "disk":
        return DiskBundler(
            base_dir=os.environ["BUNDLE_DIR"],
            public_base_url=replay_host,
        )
    if backend == "http":
        return HttpBundler(
            upload_url=os.environ["BUNDLE_UPLOAD_URL"],
            read_url=os.environ["BUNDLE_READ_URL"],
            public_base_url=replay_host,
        )
    if backend == "r2":
        return R2Bundler(
            endpoint_url=os.environ["R2_ENDPOINT_URL"],
            access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            bucket=os.environ["R2_BUCKET"],
            public_base_url=replay_host,
        )
    raise ValueError(f"unknown BUNDLER_BACKEND: {backend!r}")
