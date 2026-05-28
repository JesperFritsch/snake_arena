# services/api/api/routers/modes.py
"""Mode listings.

Modes are persistent evaluation configurations seeded in migrations/001.sql.
See docs/09_ranking_system.md.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from psycopg import Connection

from sa_common.db.modes import list_modes
from sa_common.db.users import User

from api.auth import get_current_user
from api.db import get_db
from api.schemas import ModeOut

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
            participant_count=m.participant_count,
            sim_args=m.sim_args,
            map_slug=m.map_slug,
            budget_ms=m.budget_ms,
            target_matches_per_version=m.target_matches_per_version,
            enabled=m.enabled,
        )
        for m in modes
    ]
