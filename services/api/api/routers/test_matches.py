# services/api/api/routers/test_matches.py
"""Test match endpoints.

A test match lets a user run their dev build against any submitted project.
The player slot uses dev_image_tag; opponents use submitted_image_tag.
Results are recorded with is_test=True and excluded from the leaderboard.

Live streaming: GET /test-matches/{id}/ws  (WebSocket, token query param)
  - While the match runs: streams JSON frames from the Redis pub/sub channel.
  - After the match ends: serves the stored .json.gz replay via the same socket.

Replay file: GET /test-matches/{id}/replay
  - Serves the gzip-compressed JSON replay with Content-Encoding: gzip so the
    browser's fetch decompresses it transparently.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse, Response
from psycopg import Connection

from sa_common.db.projects import get_project_meta, list_all_submitted
from sa_common.db.test_match_jobs import enqueue_test_match_job, get_test_job, TestMatchJob
from sa_common.db.users import User, get_or_create_user_by_clerk_id

from api.auth import decode_token, get_current_user
from api.db import get_db, get_pool
from api.redis import get_redis
from api.settings import Settings, get_settings
from api.schemas import TestMatchCreate, PublicProjectSummary

log = logging.getLogger(__name__)
router = APIRouter(prefix="/test-matches", tags=["test-matches"])


@router.get("/opponents", response_model=list[PublicProjectSummary])
def list_opponents(
    conn: Connection = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[PublicProjectSummary]:
    """All submitted projects across all users, for opponent selection."""
    return list_all_submitted(conn)


@router.post("", response_model=TestMatchJob, status_code=status.HTTP_202_ACCEPTED)
def enqueue(
    body: TestMatchCreate,
    conn: Connection = Depends(get_db),
    user: User = Depends(get_current_user),
) -> TestMatchJob:
    player = get_project_meta(conn, body.player_project_id)
    if player is None or player.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "player project not found")
    if player.dev_build_status != "ready":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "player project has no ready dev build — build it first",
        )

    for opp_id in body.opponent_project_ids:
        opp = get_project_meta(conn, opp_id)
        if opp is None or opp.submitted_version == 0:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                f"opponent project {opp_id} not found or has no submitted version",
            )

    job_id = enqueue_test_match_job(
        conn,
        player_project_id=body.player_project_id,
        opponent_project_ids=body.opponent_project_ids,
        sim_args=body.sim_args,
        requested_by=user.id,
    )
    job = get_test_job(conn, job_id)
    assert job is not None
    return job


@router.get("/{job_id}", response_model=TestMatchJob)
def get_job(
    job_id: int,
    conn: Connection = Depends(get_db),
    user: User = Depends(get_current_user),
) -> TestMatchJob:
    job = get_test_job(conn, job_id)
    if job is None or job.requested_by != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "test match job not found")
    return job


@router.get("/{job_id}/replay")
def get_replay(
    job_id: int,
    conn: Connection = Depends(get_db),
    user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Serve the gzip-compressed JSON replay file."""
    job = get_test_job(conn, job_id)
    if job is None or job.requested_by != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "test match job not found")
    if job.status != "success" or not job.replay_json_path:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "replay not available yet")
    if not settings.replay_dir:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "replay storage not configured")

    replay_path = settings.replay_dir / job.replay_json_path
    if not replay_path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "replay file not found on disk")

    return Response(
        content=replay_path.read_bytes(),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"},
    )


@router.websocket("/{job_id}/ws")
async def stream_test_match(
    job_id: int,
    websocket: WebSocket,
    token: str = Query(..., description="Clerk JWT for authentication"),
    settings: Settings = Depends(get_settings),
) -> None:
    """Stream sim events to the browser.

    Accepts a Clerk JWT via ?token= (WebSocket doesn't support Auth headers).
    If the match is still running: forwards frames from the Redis pub/sub channel.
    If the match is already done: replays from the .json.gz file.
    """
    await websocket.accept()

    # Auth + job ownership check (sync, run in thread to avoid blocking event loop)
    try:
        user, job = await asyncio.to_thread(_auth_and_get_job, token, job_id)
    except HTTPException as exc:
        await websocket.close(code=4000 + (exc.status_code % 1000))
        return
    except Exception:
        log.exception("WS auth failed for job %d", job_id)
        await websocket.close(code=4500)
        return

    try:
        if job.status == "success":
            await _serve_replay(websocket, job, settings)
        elif job.status == "failure":
            await websocket.send_text(json.dumps({
                "type": "error",
                "data": {"message": job.error or "match failed"},
            }))
        else:
            await _stream_live(websocket, job_id, settings)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("WS stream error for job %d", job_id)
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


# ------------------------------------------------------------------ helpers

def _auth_and_get_job(token: str, job_id: int) -> tuple[User, TestMatchJob]:
    """Sync helper: verify token and load job from DB. Runs in a thread pool."""
    claims = decode_token(token)  # raises HTTPException on invalid token
    clerk_user_id = claims.get("sub")
    email = claims.get("email")
    if not clerk_user_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token missing sub claim")
    if not email:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token missing email claim")

    with get_pool().connection() as conn:
        user = get_or_create_user_by_clerk_id(
            conn,
            clerk_user_id=clerk_user_id,
            email=email,
            display_name=claims.get("name") or email,
        )
        job = get_test_job(conn, job_id)
        if job is None or job.requested_by != user.id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "test match job not found")
        return user, job


async def _serve_replay(
    websocket: WebSocket,
    job: TestMatchJob,
    settings: Settings,
) -> None:
    """Send all stored replay messages over the WebSocket."""
    if not job.replay_json_path or not settings.replay_dir:
        await websocket.send_text(json.dumps({"type": "error", "data": {"message": "replay not available"}}))
        return

    replay_path = settings.replay_dir / job.replay_json_path
    if not replay_path.exists():
        await websocket.send_text(json.dumps({"type": "error", "data": {"message": "replay file missing"}}))
        return

    raw = await asyncio.to_thread(lambda: gzip.open(replay_path, "rb").read())
    messages: list[dict] = json.loads(raw)
    for msg in messages:
        await websocket.send_text(json.dumps(msg, separators=(",", ":")))


async def _stream_live(websocket: WebSocket, job_id: int, settings: Settings) -> None:
    """Subscribe to Redis and forward frames until STOP or disconnect."""
    redis = get_redis()
    pubsub = redis.pubsub()
    await pubsub.subscribe(f"test-match:{job_id}")
    try:
        async with asyncio.timeout(600):  # 10-minute safety cap
            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                data = message["data"]
                text = data.decode() if isinstance(data, (bytes, bytearray)) else data
                await websocket.send_text(text)
                try:
                    if json.loads(text).get("type") == "stop":
                        break
                except Exception:
                    pass
    except asyncio.TimeoutError:
        log.warning("WS stream for job %d hit 10-minute timeout", job_id)
    finally:
        await pubsub.unsubscribe(f"test-match:{job_id}")
        await redis.aclose()
