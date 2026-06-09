# services/api/api/routers/matches.py
"""Read-only match endpoints.

Ranked matches are exclusively enqueued by the scheduler daemon (see
docs/09_ranking_system.md); the API does not enqueue ranked matches. Users
exercise their dev agents via /test-matches.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from psycopg import Connection

from sa_common.db.matches import (
    MATCH_STATUSES,
    Match,
    get_match,
    get_match_participants,
    list_matches,
    list_ranked_matches_for_project,
)
from sa_common.db.users import User

from api.auth import get_current_user
from api.bundler import get_bundler
from api.db import get_db
from api.schemas import MatchDetail, ParticipantOut, RankedMatchParticipant, RankedMatchSummary

router = APIRouter(tags=["matches"])


# ---- completed matches ----------------------------------------------------

@router.get("/matches", response_model=list[Match])
def list_completed_matches(
    status_filter: str | None = Query(None, alias="status"),
    mode_id: int | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    conn: Connection = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[Match]:
    if status_filter is not None and status_filter not in MATCH_STATUSES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid status: {status_filter}")
    return list_matches(conn, status=status_filter, mode_id=mode_id, limit=limit)


@router.get("/matches/for-project", response_model=list[RankedMatchSummary])
def list_matches_for_project(
    project_id: int = Query(...),
    mode_ids: list[int] | None = Query(
        None, description="filter to one or more modes (repeat the query param)",
    ),
    limit: int = Query(20, ge=1, le=100),
    conn: Connection = Depends(get_db),
) -> list[RankedMatchSummary]:
    summaries = list_ranked_matches_for_project(conn, project_id, limit, mode_ids=mode_ids)
    return [
        RankedMatchSummary(
            id=s.id,
            match_uuid=s.match_uuid,
            status=s.status,
            mode_id=s.mode_id,
            started_at=s.started_at,
            finished_at=s.finished_at,
            bundle_key=s.bundle_key,
            participants=[
                RankedMatchParticipant(
                    seat=p.seat,
                    project_id=p.project_id,
                    project_name=p.project_name,
                    final_length=p.final_length,
                    survival_rank=p.survival_rank,
                    metrics=p.metrics,
                )
                for p in s.participants
            ],
        )
        for s in summaries
    ]


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
        mode_id=match.mode_id,
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