# services/api/api/routers/webhooks.py
"""Webhook endpoints fired by external services.

Today there's exactly one: Clerk's `user.deleted` event. We don't accept
self-service deletion via our own UI — Clerk's hosted UserProfile modal
owns the delete button, fires user.deleted to us via Svix-signed webhook,
and we cascade the gridsnake-side cleanup from here. Idempotent on
retry: if the user row is already gone we no-op.

Setup (Clerk dashboard):
  1. Webhooks → Add endpoint, URL `<DOMAIN>/webhooks/clerk`.
  2. Subscribe to `user.deleted`.
  3. Copy the signing secret to CLERK_WEBHOOK_SECRET in the API env.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from collections.abc import Mapping

import docker
from docker.errors import DockerException, ImageNotFound
from fastapi import APIRouter, Depends, HTTPException, Request, status
from psycopg import Connection

from sa_common.bundler import IBundler
from sa_common.db.users import delete_user_by_clerk_id

from api.bundler import get_bundler
from api.db import get_db
from api.settings import Settings, get_settings

log = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


# Svix's signed-webhook scheme — used by Clerk. Spec:
# https://docs.svix.com/receiving/verifying-payloads/how-manual
#
# Headers:
#   svix-id          unique message id (also part of the signed string)
#   svix-timestamp   unix seconds; we reject anything outside ±5 min to bound
#                    replay window
#   svix-signature   one or more "version,base64sig" pairs separated by
#                    spaces; current version is "v1"
#
# Secret format: "whsec_<base64>" where the base64 decodes to the HMAC key.
# Signed string: f"{svix_id}.{svix_timestamp}.{raw_body}" (raw bytes, not
# the parsed JSON — re-serialising can change byte order).
_SVIX_TOLERANCE_S = 5 * 60


def _verify_svix(secret: str, headers: Mapping[str, str], body: bytes) -> None:
    msg_id = headers.get("svix-id")
    ts = headers.get("svix-timestamp")
    sigs = headers.get("svix-signature")
    if not (msg_id and ts and sigs):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing svix-* headers")

    try:
        ts_int = int(ts)
    except ValueError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad svix-timestamp")
    if abs(time.time() - ts_int) > _SVIX_TOLERANCE_S:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "svix-timestamp out of tolerance")

    if not secret.startswith("whsec_"):
        # Misconfiguration on our side, not the caller's. 500 not 401.
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "CLERK_WEBHOOK_SECRET must start with whsec_",
        )
    key = base64.b64decode(secret.removeprefix("whsec_"))

    signed = f"{msg_id}.{ts}.".encode() + body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()

    for entry in sigs.split():
        version, _, sig = entry.partition(",")
        if version == "v1" and sig and hmac.compare_digest(expected, sig):
            return
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "no matching signature")


@router.post("/clerk", status_code=status.HTTP_204_NO_CONTENT)
async def clerk_webhook(
    request: Request,
    conn: Connection = Depends(get_db),
    settings: Settings = Depends(get_settings),
    bundler: IBundler = Depends(get_bundler),
) -> None:
    body = await request.body()
    _verify_svix(settings.clerk_webhook_secret, request.headers, body)

    try:
        event = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body is not JSON")

    event_type = event.get("type")
    if event_type != "user.deleted":
        # Subscribe to additional events in Clerk dashboard as new flows
        # are wired here; until then we acknowledge without acting so
        # Clerk doesn't retry forever.
        return

    clerk_user_id = (event.get("data") or {}).get("id")
    if not clerk_user_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "user.deleted event missing data.id"
        )

    with conn.transaction():
        artifacts = delete_user_by_clerk_id(conn, clerk_user_id)

    if not artifacts.found:
        log.info("user.deleted for unknown clerk_user_id=%s, no-op", clerk_user_id)
        return

    # Best-effort, post-commit cleanup of artifacts outside the DB. The
    # bundler treats 404 as success, so a half-completed run that retries
    # later won't double-delete; orphans here get mopped up by image GC
    # / bundle retention.
    for key in artifacts.bundle_keys:
        try:
            bundler.delete(key)
        except Exception:
            log.exception("failed to delete bundle %s after user.deleted", key)

    if artifacts.image_tags:
        try:
            client = docker.from_env()
        except DockerException:
            log.exception(
                "user.deleted: docker unavailable, leaving %d image tag(s) orphaned: %s",
                len(artifacts.image_tags),
                artifacts.image_tags,
            )
        else:
            for tag in artifacts.image_tags:
                try:
                    client.images.remove(tag, force=True, noprune=False)
                except ImageNotFound:
                    pass  # already gone, idempotent
                except DockerException:
                    log.exception("failed to remove docker image %s", tag)
