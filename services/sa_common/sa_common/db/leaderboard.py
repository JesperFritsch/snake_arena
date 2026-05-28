# services/sa_common/sa_common/db/leaderboard.py
"""Leaderboard queries.

Two views:
  - get_mode_leaderboard(mode_id) — per-mode rankings.
  - get_overall_leaderboard()     — cross-mode normalised rankings, eligibility
                                    gated on per-mode minimums.

Both count only non-test, scored success matches. See docs/09_ranking_system.md.
"""
from __future__ import annotations

from dataclasses import dataclass

import psycopg
from psycopg.rows import dict_row


@dataclass
class LeaderboardEntry:
    rank: int
    project_id: int
    project_name: str
    language: str
    user_display_name: str
    matches_played: int
    avg_score: float
    best_score: float
    avg_rank: float
    avg_length: float | None


@dataclass
class OverallLeaderboardEntry:
    rank: int
    project_id: int
    project_name: str
    language: str
    user_display_name: str
    overall_score: float           # 0..100, mean of per-mode normalised scores
    total_matches: int             # across all modes
    modes_played: int              # how many modes the player competes in


def get_mode_leaderboard(
    conn: psycopg.Connection,
    mode_id: int,
    limit: int = 100,
) -> list[LeaderboardEntry]:
    """Per-mode leaderboard, sorted by avg_score descending."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT
                p.id              AS project_id,
                p.name            AS project_name,
                p.language,
                u.display_name    AS user_display_name,
                COUNT(*)          AS matches_played,
                AVG((mp.metrics->>'score')::float)    AS avg_score,
                MAX((mp.metrics->>'score')::float)    AS best_score,
                AVG(mp.survival_rank::float)          AS avg_rank,
                AVG(mp.final_length::float)           AS avg_length
            FROM match_participants mp
            JOIN matches  m ON m.id   = mp.match_id
            JOIN projects p ON p.id   = mp.project_id
            JOIN users    u ON u.id   = p.user_id
            WHERE m.status    = 'success'
              AND m.is_test   = FALSE
              AND m.mode_id   = %s
              AND mp.metrics ? 'score'
            GROUP BY p.id, p.name, p.language, u.display_name
            ORDER BY avg_score DESC NULLS LAST
            LIMIT %s
            """,
            (mode_id, limit),
        )
        rows = cur.fetchall()

    return [
        LeaderboardEntry(
            rank=i + 1,
            project_id=row["project_id"],
            project_name=row["project_name"],
            language=row["language"],
            user_display_name=row["user_display_name"],
            matches_played=int(row["matches_played"]),
            avg_score=float(row["avg_score"] or 0),
            best_score=float(row["best_score"] or 0),
            avg_rank=float(row["avg_rank"] or 0),
            avg_length=float(row["avg_length"]) if row["avg_length"] is not None else None,
        )
        for i, row in enumerate(rows)
    ]


def get_overall_leaderboard(
    conn: psycopg.Connection,
    limit: int = 100,
) -> list[OverallLeaderboardEntry]:
    """Cross-mode leaderboard.

    Each player's avg_score per mode is normalised to [0, 100] relative to that
    mode's leader. The overall score is the mean of those normalised values.

    Eligibility: a project must have played at least CEIL(target/2) matches in
    EVERY enabled mode. This keeps the board fair — players who skip easy modes
    can't farm rank by dominating one mode.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            WITH per_mode AS (
                SELECT
                    m.mode_id,
                    mp.project_id,
                    COUNT(*)                            AS matches_played,
                    AVG((mp.metrics->>'score')::float)  AS avg_score
                FROM match_participants mp
                JOIN matches m ON m.id = mp.match_id
                WHERE m.status  = 'success'
                  AND m.is_test = FALSE
                  AND m.mode_id IS NOT NULL
                  AND mp.metrics ? 'score'
                GROUP BY m.mode_id, mp.project_id
            ),
            mode_leaders AS (
                SELECT mode_id, MAX(avg_score) AS top
                FROM per_mode
                GROUP BY mode_id
            ),
            -- Normalise each (project, mode) avg to 0..100 of the mode leader.
            normalised AS (
                SELECT
                    pm.project_id,
                    pm.mode_id,
                    pm.matches_played,
                    CASE WHEN ml.top IS NULL OR ml.top = 0 THEN 0
                         ELSE 100.0 * pm.avg_score / ml.top
                    END AS pct
                FROM per_mode pm
                JOIN mode_leaders ml USING (mode_id)
            ),
            enabled_modes AS (
                SELECT id, CEIL(target_matches_per_version::float / 2)::int AS min_required
                FROM modes
                WHERE enabled = TRUE
            ),
            enabled_count AS (
                SELECT COUNT(*) AS n FROM enabled_modes
            ),
            -- A project is eligible iff it has min_required matches in EVERY
            -- enabled mode. Counted by joining per_mode to enabled_modes and
            -- checking the satisfied-rows count equals enabled_count.n.
            eligible AS (
                SELECT pm.project_id
                FROM per_mode pm
                JOIN enabled_modes em ON em.id = pm.mode_id
                WHERE pm.matches_played >= em.min_required
                GROUP BY pm.project_id
                HAVING COUNT(*) = (SELECT n FROM enabled_count)
            )
            SELECT
                p.id              AS project_id,
                p.name            AS project_name,
                p.language,
                u.display_name    AS user_display_name,
                AVG(n.pct)        AS overall_score,
                SUM(n.matches_played) AS total_matches,
                COUNT(*)          AS modes_played
            FROM normalised n
            JOIN eligible  e ON e.project_id = n.project_id
            JOIN projects  p ON p.id         = n.project_id
            JOIN users     u ON u.id         = p.user_id
            GROUP BY p.id, p.name, p.language, u.display_name
            ORDER BY overall_score DESC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()

    return [
        OverallLeaderboardEntry(
            rank=i + 1,
            project_id=row["project_id"],
            project_name=row["project_name"],
            language=row["language"],
            user_display_name=row["user_display_name"],
            overall_score=float(row["overall_score"] or 0),
            total_matches=int(row["total_matches"]),
            modes_played=int(row["modes_played"]),
        )
        for i, row in enumerate(rows)
    ]
