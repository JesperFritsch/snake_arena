# services/api/api/routers/modes.py
"""Mode and mode-group listings.

Modes are persistent evaluation configurations seeded in migrations/001.sql.
Groups bundle multiple modes under one leaderboard tab. See docs/09_ranking_system.md.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from psycopg import Connection

from sa_common.db.mode_groups import list_groups
from sa_common.db.modes import list_modes
from sa_common.db.users import User

from api.auth import get_current_user
from api.db import get_db
from api.schemas import GroupOut, ModeOut

router = APIRouter(tags=["modes"])


@router.get("/modes", response_model=list[ModeOut])
def list_all_modes(
    enabled_only: bool = True,
    conn: Connection = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[ModeOut]:
    modes = list_modes(conn, enabled_only=enabled_only)
    return [
        ModeOut(
            id=m.id,
            slug=m.slug,
            name=m.name,
            description=m.description,
            group_slug=m.group_slug,
            participant_count=m.participant_count,
            sim_args=m.sim_args,
            map_slug=m.map_slug,
            avg_budget_ms=m.avg_budget_ms,
            target_matches_per_version=m.target_matches_per_version,
            enabled=m.enabled,
        )
        for m in modes
    ]


@router.get("/mode-groups", response_model=list[GroupOut])
def list_all_groups(
    conn: Connection = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[GroupOut]:
    return [
        GroupOut(
            slug=g.slug,
            name=g.name,
            description=g.description,
            sort_order=g.sort_order,
        )
        for g in list_groups(conn)
    ]
