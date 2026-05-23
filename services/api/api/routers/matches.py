# services/api/api/routers/matches.py
"""Match endpoints: enqueue match jobs, read job status, read completed
matches and their participants.

Enqueuing a match writes a row to match_jobs and returns immediately. The
orchestrator (a separate container) claims the job, runs the match in the
runner, and records the result in `matches` / `match_participants`. The API
never runs a match itself.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from psycopg import Connection

from sa_common.db.match_jobs import (
    JOB_STATUSES,
    MatchJob,
    cancel_job,
    enqueue_match_job,
    get_job,
    list_jobs,
)
from sa_common.db.matches import (
    MATCH_MODES,
    MATCH_STATUSES,
    Match,
    get_match,
    get_match_participants,
    list_matches,
)
from sa_common.db.projects import get_project_meta
from sa_common.db.users import User

from api.auth import get_current_user
from api.bundler import get_bundler
from api.db import get_db
from api.schemas import MatchDetail, MatchJobCreate, ParticipantOut

router = APIRouter(tags=["matches"])


# ---- match jobs -----------------------------------------------------------

@router.post("/match-jobs", response_model=MatchJob, status_code=status.HTTP_202_ACCEPTED)
def enqueue(
    body: MatchJobCreate,
    conn: Connection = Depends(get_db),
    user: User = Depends(get_current_user),
) -> MatchJob:
    # v0 authorization: you may only enqueue matches between projects you own.
    # Public/ranked matchmaking that pits arbitrary submitted agents against
    # each other will relax this later.
    for project_id in body.project_ids:
        meta = get_project_meta(conn, project_id)
        if meta is None or meta.user_id != user.id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"project {project_id} not found")
        if meta.submitted_version == 0:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"project {project_id} has no submitted version to play",
            )

    job_id = enqueue_match_job(
        conn,
        project_ids=body.project_ids,
        sim_args=body.sim_args,
        requested_by=user.id,
    )
    job = get_job(conn, job_id)
    assert job is not None
    return job


@router.get("/match-jobs", response_model=list[MatchJob])
def list_match_jobs(
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(20, ge=1, le=100),
    conn: Connection = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[MatchJob]:
    if status_filter is not None and status_filter not in JOB_STATUSES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid status: {status_filter}")
    jobs = list_jobs(conn, status=status_filter, limit=limit)
    # list_jobs has no user filter; scope to the caller here.
    return [j for j in jobs if j.requested_by == user.id]


@router.get("/match-jobs/{job_id}", response_model=MatchJob)
def get_match_job(
    job_id: int,
    conn: Connection = Depends(get_db),
    user: User = Depends(get_current_user),
) -> MatchJob:
    job = get_job(conn, job_id)
    if job is None or job.requested_by != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    return job


@router.delete("/match-jobs/{job_id}", status_code=status.HTTP_200_OK)
def cancel_match_job(
    job_id: int,
    conn: Connection = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    job = get_job(conn, job_id)
    if job is None or job.requested_by != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    cancelled = cancel_job(conn, job_id)
    return {"cancelled": cancelled}


# ---- completed matches ----------------------------------------------------

@router.get("/matches", response_model=list[Match])
def list_completed_matches(
    status_filter: str | None = Query(None, alias="status"),
    mode: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    conn: Connection = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[Match]:
    if status_filter is not None and status_filter not in MATCH_STATUSES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid status: {status_filter}")
    if mode is not None and mode not in MATCH_MODES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid mode: {mode}")
    return list_matches(conn, status=status_filter, mode=mode, limit=limit)


@router.get("/matches/{match_id}", response_model=MatchDetail)
def get_match_detail(
    match_id: int,
    conn: Connection = Depends(get_db),
    _: User = Depends(get_current_user),
) -> MatchDetail:
    match = get_match(conn, match_id)
    if match is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "match not found")
    participants = get_match_participants(conn, match_id)
    return MatchDetail(
        id=match.id,
        match_uuid=match.match_uuid,
        status=match.status,
        mode=match.mode,
        sim_args=match.sim_args,
        started_at=match.started_at,
        finished_at=match.finished_at,
        bundle_key=match.bundle_key,
        error=match.error,
        participants=[
            ParticipantOut(
                seat=p.seat,
                project_id=p.project_id,
                project_version=p.project_version,
                final_length=p.final_length,
                fatal_step=p.fatal_step,
                survival_rank=p.survival_rank,
                killed_by_budget=p.killed_by_budget,
                metrics=p.metrics,
            )
            for p in participants
        ],
    )


@router.get("/matches/{match_id}/bundle-url")
def get_match_bundle_url(
    match_id: int,
    conn: Connection = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    """Return the URL the browser should fetch for a ranked match's bundle.

    Mirrors the test-match bundle endpoint: the bundler resolves the stored
    key to a fetchable URL (file-server now, presigned R2 later)."""
    match = get_match(conn, match_id)
    if match is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "match not found")
    if not match.bundle_key:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "match has no bundle")
    try:
        url = get_bundler().url(match.bundle_key)
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return {"url": url}