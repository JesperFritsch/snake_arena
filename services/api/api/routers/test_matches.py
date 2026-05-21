# services/api/api/routers/test_matches.py
"""Test match endpoints.

A test match lets a user run their dev build against any submitted project.
The player slot uses dev_image_tag; opponents use submitted_image_tag.
Results are recorded with is_test=True and excluded from the leaderboard.

Live streaming: GET /test-matches/{id}/ws  (WebSocket, token query param)
  - Only used while the match is queued or running.
  - Streams JSON frames from the Redis pub/sub channel until a "stop" frame.

Bundle URL: GET /test-matches/{id}/bundle-url
  - Returns { url } pointing at the match bundle on the file host.
  - The bundle is a ZIP containing replay.json, analysis.json, run.log.
  - In dev the host is the nginx file-server; in prod swap REPLAY_HOST for R2.
"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from psycopg import Connection

from sa_common.db.projects import get_project_meta, get_project_names, list_all_submitted
from sa_common.db.test_match_jobs import (
    enqueue_test_match_job,
    get_test_job,
    list_test_jobs_for_project,
    TestMatchJob,
)
from sa_common.db.users import User, get_or_create_user_by_clerk_id

from api.auth import decode_token, get_current_user
from api.db import get_db, get_pool
from api.redis import get_redis
from api.settings import Settings, get_settings
from api.schemas import TestMatchCreate, PublicProjectSummary

log = logging.getLogger(__name__)
router = APIRouter(prefix="/test-matches", tags=["test-matches"])


def _add_names(conn: Connection, job: TestMatchJob) -> TestMatchJob:
    """Populate participant_names: [player, opp1, opp2, ...] ordered by seat."""
    all_ids = [job.player_project_id] + list(job.opponent_project_ids)
    names = get_project_names(conn, all_ids)
    job.participant_names = [names.get(pid, "?") for pid in all_ids]
    return job


@router.get("/opponents", response_model=list[PublicProjectSummary])
def list_opponents(
    conn: Connection = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[PublicProjectSummary]:
    """All submitted projects across all users, for opponent selection."""
    return list_all_submitted(conn)


@router.get("", response_model=list[TestMatchJob])
def list_jobs_for_project(
    player_project_id: int = Query(..., description="Filter by player project"),
    limit: int = Query(10, ge=1, le=50),
    conn: Connection = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[TestMatchJob]:
    """Last N test match jobs for the given project (newest first)."""
    player = get_project_meta(conn, player_project_id)
    if player is None or player.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")
    jobs = list_test_jobs_for_project(conn, player_project_id, limit=limit)
    return [_add_names(conn, j) for j in jobs]


@router.post("", response_model=TestMatchJob, status_code=status.HTTP_202_ACCEPTED)
def enqueue(
    body: TestMatchCreate,
    conn: Connection = Depends(get_db),
    user: User = Depends(get_current_user),
) -> TestMatchJob:
    player = get_project_meta(conn, body.player_project_id)
    if player is None or player.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "player project not found")

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
    return _add_names(conn, job)


@router.get("/{job_id}", response_model=TestMatchJob)
def get_job(
    job_id: int,
    conn: Connection = Depends(get_db),
    user: User = Depends(get_current_user),
) -> TestMatchJob:
    job = get_test_job(conn, job_id)
    if job is None or job.requested_by != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "test match job not found")
    return _add_names(conn, job)


@router.get("/{job_id}/bundle-url")
def get_bundle_url(
    job_id: int,
    conn: Connection = Depends(get_db),
    user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Return the URL the browser should fetch to download the match bundle."""
    job = get_test_job(conn, job_id)
    if job is None or job.requested_by != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "test match job not found")
    if job.status != "success" or not job.bundle_path:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "bundle not available yet")
    if not settings.replay_host:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "replay host not configured")
    url = f"{settings.replay_host.rstrip('/')}/{job.bundle_path}"
    return {"url": url}


@router.websocket("/{job_id}/ws")
async def stream_test_match(
    job_id: int,
    websocket: WebSocket,
    token: str = Query(..., description="Clerk JWT for authentication"),
    settings: Settings = Depends(get_settings),
) -> None:
    """Stream live sim events while a match is running.

    Only handles queued/running matches. Completed matches should be fetched
    as a bundle via GET /{job_id}/bundle-url instead.
    """
    await websocket.accept()

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
        if job.status in ("queued", "running"):
            await _stream_live(websocket, job_id, settings)
        elif job.status == "failure":
            await websocket.send_text(json.dumps({
                "type": "error",
                "data": {"message": job.error or "match failed"},
            }))
        else:
            # success or unknown — client should use /bundle-url instead
            await websocket.send_text(json.dumps({
                "type": "error",
                "data": {"message": "match already completed — fetch bundle instead"},
            }))
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
    claims = decode_token(token)
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


async def _stream_live(websocket: WebSocket, job_id: int, settings: Settings) -> None:
    """Subscribe to Redis and forward frames until STOP or disconnect."""
    redis = get_redis()
    pubsub = redis.pubsub()
    await pubsub.subscribe(f"test-match:{job_id}")
    try:
        async with asyncio.timeout(600):
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
