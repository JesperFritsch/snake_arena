# services/api/api/routers/leaderboard.py
"""Leaderboard endpoints.

  GET /leaderboard/overall          — cross-group normalised ranking
  GET /leaderboard/group?group=…    — per-group ranking (aggregate of modes in group)
  GET /leaderboard?mode=<slug>      — per-mode ranking (sub-tab drill-in)
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from psycopg import Connection

from sa_common.db.leaderboard import (
    get_group_leaderboard,
    get_mode_leaderboard,
    get_overall_leaderboard,
)
from sa_common.db.mode_groups import get_group
from sa_common.db.modes import get_mode_by_slug
from api.db import get_db
from api.schemas import (
    GroupLeaderboardEntry,
    LeaderboardEntry,
    OverallLeaderboardEntry,
)

router = APIRouter(tags=["leaderboard"])


@router.get("/leaderboard/overall", response_model=list[OverallLeaderboardEntry])
def overall_leaderboard(
    limit: int = Query(100, ge=1, le=500),
    conn: Connection = Depends(get_db),
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


@router.get("/leaderboard/group", response_model=list[GroupLeaderboardEntry])
def group_leaderboard(
    group: str = Query(..., description="group slug, e.g. solo"),
    limit: int = Query(100, ge=1, le=500),
    conn: Connection = Depends(get_db),
) -> list[GroupLeaderboardEntry]:
    g = get_group(conn, group)
    if g is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"group not found: {group}")
    entries = get_group_leaderboard(conn, group_slug=g.slug, limit=limit)
    return [
        GroupLeaderboardEntry(
            rank=e.rank,
            project_id=e.project_id,
            project_name=e.project_name,
            language=e.language,
            user_display_name=e.user_display_name,
            group_score=e.group_score,
            matches_played=e.matches_played,
            modes_played=e.modes_played,
        )
        for e in entries
    ]


@router.get("/leaderboard", response_model=list[LeaderboardEntry])
def mode_leaderboard(
    mode: str = Query(..., description="mode slug, e.g. multi-4-standard"),
    limit: int = Query(100, ge=1, le=500),
    conn: Connection = Depends(get_db),
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
            score=e.score,
            category_breakdown=e.category_breakdown,
        )
        for e in entries
    ]
