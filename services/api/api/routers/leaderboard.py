# services/api/api/routers/leaderboard.py
"""Leaderboard endpoints.

  GET /leaderboard/overall          — cross-mode normalised ranking
  GET /leaderboard?mode=<slug>      — per-mode ranking
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from psycopg import Connection

from sa_common.db.leaderboard import get_mode_leaderboard, get_overall_leaderboard
from sa_common.db.modes import get_mode_by_slug
from sa_common.db.users import User

from api.auth import get_current_user
from api.db import get_db
from api.schemas import LeaderboardEntry, OverallLeaderboardEntry

router = APIRouter(tags=["leaderboard"])


@router.get("/leaderboard/overall", response_model=list[OverallLeaderboardEntry])
def overall_leaderboard(
    limit: int = Query(100, ge=1, le=500),
    conn: Connection = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[OverallLeaderboardEntry]:
    entries = get_overall_leaderboard(conn, limit=limit)
    return [
        OverallLeaderboardEntry(
            rank=e.rank,
            project_id=e.project_id,
            project_name=e.project_name,
            language=e.language,
            user_display_name=e.user_display_name,
            overall_score=e.overall_score,
            total_matches=e.total_matches,
            modes_played=e.modes_played,
        )
        for e in entries
    ]


@router.get("/leaderboard", response_model=list[LeaderboardEntry])
def mode_leaderboard(
    mode: str = Query(..., description="mode slug, e.g. multi-4-standard"),
    limit: int = Query(100, ge=1, le=500),
    conn: Connection = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[LeaderboardEntry]:
    m = get_mode_by_slug(conn, mode)
    if m is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"mode not found: {mode}")
    entries = get_mode_leaderboard(conn, mode_id=m.id, limit=limit)
    return [
        LeaderboardEntry(
            rank=e.rank,
            project_id=e.project_id,
            project_name=e.project_name,
            language=e.language,
            user_display_name=e.user_display_name,
            matches_played=e.matches_played,
            avg_score=e.avg_score,
            best_score=e.best_score,
            avg_rank=e.avg_rank,
            avg_length=e.avg_length,
        )
        for e in entries
    ]
