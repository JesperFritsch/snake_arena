# services/sa_common/sa_common/db/modes.py
"""DB layer for ranked-match modes.

A mode is one persistent evaluation configuration (participant count, sim
args, scoring weights, target matches per submission version). Seeded in
migrations/001.sql. Read by scheduler, matchmaker, scorer, API.

See docs/09_ranking_system.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row


@dataclass(slots=True)
class Mode:
    id: int
    slug: str
    name: str
    description: str | None
    participant_count: int
    sim_args: dict[str, Any]
    map_slug: str | None
    budget_ms: float
    scoring_config: dict[str, Any]
    target_matches_per_version: int
    enabled: bool


_MODE_COLUMNS = """
    id, slug, name, description,
    participant_count, sim_args, map_slug,
    budget_ms, scoring_config,
    target_matches_per_version, enabled
"""


def _row_to_mode(row: dict[str, Any]) -> Mode:
    return Mode(
        id=row["id"],
        slug=row["slug"],
        name=row["name"],
        description=row["description"],
        participant_count=row["participant_count"],
        sim_args=row["sim_args"],
        map_slug=row["map_slug"],
        budget_ms=float(row["budget_ms"]),
        scoring_config=row["scoring_config"],
        target_matches_per_version=row["target_matches_per_version"],
        enabled=row["enabled"],
    )


def list_modes(conn: psycopg.Connection, enabled_only: bool = True) -> list[Mode]:
    where = "WHERE enabled = TRUE" if enabled_only else ""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(f"SELECT {_MODE_COLUMNS} FROM modes {where} ORDER BY id")
        return [_row_to_mode(r) for r in cur.fetchall()]


def get_mode(conn: psycopg.Connection, mode_id: int) -> Mode | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"SELECT {_MODE_COLUMNS} FROM modes WHERE id = %s", (mode_id,),
        )
        row = cur.fetchone()
        return _row_to_mode(row) if row else None


def get_mode_by_slug(conn: psycopg.Connection, slug: str) -> Mode | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"SELECT {_MODE_COLUMNS} FROM modes WHERE slug = %s", (slug,),
        )
        row = cur.fetchone()
        return _row_to_mode(row) if row else None
