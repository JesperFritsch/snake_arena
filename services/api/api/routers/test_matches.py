# services/api/api/routers/test_matches.py
"""Test match endpoints.

A test match lets a user run their dev build against any submitted project.
The player slot uses dev_image_tag; opponents use submitted_image_tag.
Results are recorded with is_test=True and excluded from the leaderboard.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from psycopg import Connection

from sa_common.db.projects import get_project_meta, list_all_submitted
from sa_common.db.test_match_jobs import enqueue_test_match_job, get_test_job, TestMatchJob
from sa_common.db.users import User

from api.auth import get_current_user
from api.db import get_db
from api.schemas import TestMatchCreate, PublicProjectSummary

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
    # Player project must be owned by the caller and have a ready dev build.
    player = get_project_meta(conn, body.player_project_id)
    if player is None or player.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "player project not found")
    if player.dev_build_status != "ready":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "player project has no ready dev build — build it first",
        )

    # Opponents just need a submitted version (any user).
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
