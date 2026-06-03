# services/sa_common/sa_common/db/leaderboard.py
"""Leaderboard queries.

Three views, all built on `compute_mode_scores`:

  - get_mode_leaderboard(mode_id)        — per-mode rankings (drill-in).
  - get_group_leaderboard(group_slug)    — per-group rankings; mean of
                                           the player's per-mode scores
                                           across the group's modes.
  - get_overall_leaderboard()            — cross-group rankings; per-mode
                                           scores averaged within each
                                           group, then averaged across
                                           groups (so each group gets
                                           equal weight). Eligibility
                                           requires a non-None score in
                                           every enabled mode.

All scores are in [0, 1]: the frontend converts to percent for display.
No `× 100 / mode_leader` rescaling — modes share a canonical scoring kind
and category list, so per-mode scores are already comparable.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row

from sa_common.db.agent_scores import AgentScoreRow, compute_mode_scores


@dataclass(slots=True)
class LeaderboardEntry:
    rank: int
    project_id: int
    project_name: str
    language: str
    user_display_name: str
    matches_played: int
    score: float                        # 0..1
    category_breakdown: dict[str, Any]  # {<category>: {"raw": ..., "rank"?: ...}}


@dataclass(slots=True)
class GroupLeaderboardEntry:
    rank: int
    project_id: int
    project_name: str
    language: str
    user_display_name: str
    group_score: float          # 0..1, mean of per-mode scores in the group
    matches_played: int         # sum across modes in the group
    modes_played: int           # distinct modes in the group the player has scored in


@dataclass(slots=True)
class OverallLeaderboardEntry:
    rank: int
    project_id: int
    project_name: str
    language: str
    user_display_name: str
    overall_score: float        # 0..1, mean of per-group scores
    total_matches: int          # across all modes
    modes_played: int           # how many modes the player competes in


@dataclass(slots=True)
class _ProjectMeta:
    name: str
    language: str
    user_display_name: str


def _fetch_project_meta(
    conn: psycopg.Connection, project_ids: list[int],
) -> dict[int, _ProjectMeta]:
    if not project_ids:
        return {}
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT p.id, p.name, p.language, u.display_name
            FROM projects p
            JOIN users    u ON u.id = p.user_id
            WHERE p.id = ANY(%s)
            """,
            (project_ids,),
        )
        return {
            r["id"]: _ProjectMeta(
                name=r["name"],
                language=r["language"],
                user_display_name=r["display_name"],
            )
            for r in cur.fetchall()
        }


def get_mode_leaderboard(
    conn: psycopg.Connection,
    mode_id: int,
    limit: int = 100,
) -> list[LeaderboardEntry]:
    """Per-mode leaderboard, sorted by score descending.

    Unqualified agents (compute_mode_scores returns score=None) are filtered
    out — the per-mode view exists for drilling into a settled ranking, not
    for showing in-progress agents.
    """
    rows = [r for r in compute_mode_scores(conn, mode_id) if r.score is not None]
    rows.sort(key=lambda r: r.score, reverse=True)
    rows = rows[:limit]

    meta = _fetch_project_meta(conn, [r.project_id for r in rows])
    return [
        LeaderboardEntry(
            rank=i + 1,
            project_id=r.project_id,
            project_name=meta[r.project_id].name,
            language=meta[r.project_id].language,
            user_display_name=meta[r.project_id].user_display_name,
            matches_played=r.matches_played,
            score=float(r.score),
            category_breakdown=r.category_breakdown,
        )
        for i, r in enumerate(rows)
        if r.project_id in meta
    ]


def _list_modes_in_group(
    conn: psycopg.Connection, group_slug: str,
) -> list[int]:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM modes WHERE group_slug = %s", (group_slug,))
        return [r[0] for r in cur.fetchall()]


def _list_enabled_mode_ids(conn: psycopg.Connection) -> list[int]:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM modes WHERE enabled = TRUE")
        return [r[0] for r in cur.fetchall()]


def _mode_group_keys(conn: psycopg.Connection) -> dict[int, str]:
    """For each enabled mode, return the group key used to bucket its score
    in the overall view. Modes without a group get a synthetic
    `'mode:<id>'` key so they form a one-element group of equal weight."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, group_slug FROM modes WHERE enabled = TRUE",
        )
        return {
            mid: (slug if slug is not None else f"mode:{mid}")
            for mid, slug in cur.fetchall()
        }


def get_group_leaderboard(
    conn: psycopg.Connection,
    group_slug: str,
    limit: int = 100,
) -> list[GroupLeaderboardEntry]:
    """Per-group leaderboard: mean of the player's qualified per-mode
    scores within the group."""
    mode_ids = _list_modes_in_group(conn, group_slug)
    if not mode_ids:
        return []

    # Accumulate per-project across modes in the group.
    score_sum: dict[int, float] = {}
    matches_sum: dict[int, int] = {}
    modes_count: dict[int, int] = {}
    for mid in mode_ids:
        for r in compute_mode_scores(conn, mid):
            if r.score is None:
                continue
            score_sum[r.project_id]   = score_sum.get(r.project_id, 0.0) + r.score
            matches_sum[r.project_id] = matches_sum.get(r.project_id, 0) + r.matches_played
            modes_count[r.project_id] = modes_count.get(r.project_id, 0) + 1

    if not score_sum:
        return []

    project_ids = list(score_sum.keys())
    project_ids.sort(key=lambda pid: score_sum[pid] / modes_count[pid], reverse=True)
    project_ids = project_ids[:limit]

    meta = _fetch_project_meta(conn, project_ids)
    return [
        GroupLeaderboardEntry(
            rank=i + 1,
            project_id=pid,
            project_name=meta[pid].name,
            language=meta[pid].language,
            user_display_name=meta[pid].user_display_name,
            group_score=score_sum[pid] / modes_count[pid],
            matches_played=matches_sum[pid],
            modes_played=modes_count[pid],
        )
        for i, pid in enumerate(project_ids)
        if pid in meta
    ]


def get_overall_leaderboard(
    conn: psycopg.Connection,
    limit: int = 100,
) -> list[OverallLeaderboardEntry]:
    """Cross-group leaderboard. Eligibility: non-None score in every
    enabled mode (intentional — overall qualification means qualified
    against every yardstick)."""
    group_key_by_mode = _mode_group_keys(conn)
    enabled_mode_ids = list(group_key_by_mode.keys())
    if not enabled_mode_ids:
        return []
    n_enabled = len(enabled_mode_ids)

    # Per project: {group_key: [per-mode scores in this group]}, total
    # matches, total mode count, and number of qualified enabled modes.
    per_project_groups: dict[int, dict[str, list[float]]] = {}
    per_project_matches: dict[int, int] = {}
    per_project_modes: dict[int, int] = {}
    per_project_qualified: dict[int, int] = {}

    for mid in enabled_mode_ids:
        gkey = group_key_by_mode[mid]
        for r in compute_mode_scores(conn, mid):
            if r.score is None:
                continue
            per_project_groups.setdefault(r.project_id, {}).setdefault(gkey, []).append(r.score)
            per_project_matches[r.project_id]   = per_project_matches.get(r.project_id, 0) + r.matches_played
            per_project_modes[r.project_id]     = per_project_modes.get(r.project_id, 0) + 1
            per_project_qualified[r.project_id] = per_project_qualified.get(r.project_id, 0) + 1

    eligible_ids = [pid for pid, n in per_project_qualified.items() if n == n_enabled]
    if not eligible_ids:
        return []

    overall_score = {
        pid: sum(
            sum(scores) / len(scores)
            for scores in per_project_groups[pid].values()
        ) / len(per_project_groups[pid])
        for pid in eligible_ids
    }

    eligible_ids.sort(key=lambda pid: overall_score[pid], reverse=True)
    eligible_ids = eligible_ids[:limit]

    meta = _fetch_project_meta(conn, eligible_ids)
    return [
        OverallLeaderboardEntry(
            rank=i + 1,
            project_id=pid,
            project_name=meta[pid].name,
            language=meta[pid].language,
            user_display_name=meta[pid].user_display_name,
            overall_score=overall_score[pid],
            total_matches=per_project_matches[pid],
            modes_played=per_project_modes[pid],
        )
        for i, pid in enumerate(eligible_ids)
        if pid in meta
    ]


__all__ = [
    "AgentScoreRow",
    "LeaderboardEntry",
    "GroupLeaderboardEntry",
    "OverallLeaderboardEntry",
    "get_mode_leaderboard",
    "get_group_leaderboard",
    "get_overall_leaderboard",
]
