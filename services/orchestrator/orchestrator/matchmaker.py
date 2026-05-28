# services/orchestrator/orchestrator/matchmaker.py
"""Pick which submitted project versions should play together in a given mode.

A submission is "underplayed" in a mode when the number of *distinct opponent
versions* it has played in that mode is below the mode's target. The matchmaker
favours filling those gaps and introducing new pairings.

The matchmaker is a pure function over the DB state:
    - input:  a Mode + the list of underplayed (project_id, version) pairs.
    - output: one group of `mode.participant_count` versions to play together,
              or None if a group cannot be assembled.

It does not check queue depth, mark anything as scheduled, or care about
recency windows. Those concerns live in the scheduler.

For solo modes (participant_count == 1), the scheduler enqueues one job per
underplayed version directly — the matchmaker is not consulted.

See docs/09_ranking_system.md.
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass

import psycopg

from sa_common.db.modes import Mode

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class VersionRef:
    """A (project_id, version) pair — the durable identity of a submission."""
    project_id: int
    version: int


@dataclass(slots=True)
class VersionStats:
    """Aggregated play stats for one VersionRef in one mode."""
    ref: VersionRef
    matches_played: int
    distinct_opponents: int


def list_underplayed_versions(
    conn: psycopg.Connection,
    mode: Mode,
) -> list[VersionStats]:
    """Return submitted versions that haven't hit the mode's effective target.

    Multi modes: a version is underplayed when the distinct opponent versions
    it has played OR is about to play (queued/running) is less than the
    effective target. Effective target = min(mode.target, N - 1) where N is
    the number of currently-submitted projects, because you can never have
    more distinct opponents than there are other projects.

    Solo modes: count matches played + in-flight in this mode, capped at
    mode.target.

    Counting in-flight match_jobs (queued/running) prevents the scheduler
    from filling its per-mode queue cap with five copies of the same pairing
    in a single tick. Without it, queued matches are invisible to the
    matchmaker and it re-picks the same seed/opponent.

    Result is sorted least-played first.
    """
    if mode.participant_count == 1:
        return _underplayed_solo(conn, mode)
    return _underplayed_multi(conn, mode)


def _count_submitted(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM projects WHERE submitted_version > 0")
        row = cur.fetchone()
        return int(row[0]) if row else 0


def _underplayed_solo(conn: psycopg.Connection, mode: Mode) -> list[VersionStats]:
    """Solo modes: each version needs `target` matches (success or in-flight)."""
    target = mode.target_matches_per_version
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH submitted AS (
                SELECT id AS project_id, submitted_version AS version, submitted_at
                FROM projects
                WHERE submitted_version > 0
            ),
            played AS (
                -- Success matches.
                SELECT mp.project_id, mp.project_version
                FROM match_participants mp
                JOIN matches m ON m.id = mp.match_id
                              AND m.mode_id = %(mode_id)s
                              AND m.status = 'success'
                UNION ALL
                -- In-flight: each queued/running job containing this project
                -- counts as one upcoming match (project_ids[] holds it once).
                SELECT pid, p.submitted_version
                FROM match_jobs mj
                CROSS JOIN LATERAL unnest(mj.project_ids) AS pid
                JOIN projects p ON p.id = pid AND p.submitted_version > 0
                WHERE mj.mode_id = %(mode_id)s
                  AND mj.status IN ('queued', 'running')
            )
            SELECT s.project_id, s.version, COUNT(p.project_id) AS played
            FROM submitted s
            LEFT JOIN played p
                   ON p.project_id = s.project_id
                  AND p.project_version = s.version
            GROUP BY s.project_id, s.version, s.submitted_at
            HAVING COUNT(p.project_id) < %(target)s
            ORDER BY played ASC, s.submitted_at DESC
            """,
            {"mode_id": mode.id, "target": target},
        )
        return [
            VersionStats(
                ref=VersionRef(project_id=pid, version=ver),
                matches_played=played,
                distinct_opponents=0,  # not meaningful in solo
            )
            for pid, ver, played in cur.fetchall()
        ]


def _underplayed_multi(conn: psycopg.Connection, mode: Mode) -> list[VersionStats]:
    """Multi modes: each version needs `effective_target` distinct opponents.

    effective_target = min(mode.target, N - 1) — you can't have more distinct
    opponents than there are other submitted projects, so a higher mode.target
    is unreachable and would loop forever.
    """
    n_submitted = _count_submitted(conn)
    if n_submitted < mode.participant_count:
        return []
    effective_target = min(mode.target_matches_per_version, n_submitted - 1)
    if effective_target <= 0:
        return []

    with conn.cursor() as cur:
        cur.execute(
            """
            WITH submitted AS (
                SELECT id AS project_id, submitted_version AS version, submitted_at
                FROM projects
                WHERE submitted_version > 0
            ),
            -- Pairings already in match_participants (success matches only).
            played_success AS (
                SELECT mp_self.project_id, mp_self.project_version,
                       mp_other.project_id AS opp_project,
                       mp_other.project_version AS opp_version
                FROM match_participants mp_self
                JOIN matches m
                  ON m.id = mp_self.match_id
                 AND m.mode_id = %(mode_id)s
                 AND m.status = 'success'
                JOIN match_participants mp_other
                  ON mp_other.match_id = m.id
                 AND mp_other.seat != mp_self.seat
            ),
            -- Pairings queued or running (about to happen). Use each
            -- opponent's CURRENT submitted_version because that's what the
            -- runner will resolve when it dispatches.
            in_flight AS (
                SELECT self_p.id AS project_id,
                       self_p.submitted_version AS project_version,
                       other_p.id AS opp_project,
                       other_p.submitted_version AS opp_version
                FROM match_jobs mj
                CROSS JOIN LATERAL unnest(mj.project_ids) AS self_pid
                CROSS JOIN LATERAL unnest(mj.project_ids) AS other_pid
                JOIN projects self_p
                  ON self_p.id = self_pid AND self_p.submitted_version > 0
                JOIN projects other_p
                  ON other_p.id = other_pid AND other_p.submitted_version > 0
                WHERE mj.mode_id = %(mode_id)s
                  AND mj.status IN ('queued', 'running')
                  AND self_pid <> other_pid
            ),
            opponent_pairs AS (
                SELECT * FROM played_success
                UNION
                SELECT * FROM in_flight
            )
            SELECT s.project_id,
                   s.version,
                   COUNT(op.opp_project) AS matches_played,
                   COUNT(DISTINCT (op.opp_project, op.opp_version)) AS distinct_opponents
            FROM submitted s
            LEFT JOIN opponent_pairs op
                   ON op.project_id = s.project_id
                  AND op.project_version = s.version
            GROUP BY s.project_id, s.version, s.submitted_at
            HAVING COUNT(DISTINCT (op.opp_project, op.opp_version)) < %(target)s
            ORDER BY distinct_opponents ASC,
                     matches_played    ASC,
                     s.submitted_at    DESC
            """,
            {"mode_id": mode.id, "target": effective_target},
        )
        return [
            VersionStats(
                ref=VersionRef(project_id=pid, version=ver),
                matches_played=played,
                distinct_opponents=opp,
            )
            for pid, ver, played, opp in cur.fetchall()
        ]


def _list_played_opponents(
    conn: psycopg.Connection,
    mode_id: int,
    ref: VersionRef,
) -> set[VersionRef]:
    """Versions this version has already played OR is about to play in this mode.

    Includes both success matches (durable history) and queued/running jobs
    (in-flight), so within a single scheduler tick pick_match_group won't
    repeatedly re-queue the same pairing.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT mp_other.project_id, mp_other.project_version
            FROM match_participants mp_self
            JOIN matches m
              ON m.id = mp_self.match_id
             AND m.mode_id = %s
             AND m.status = 'success'
            JOIN match_participants mp_other
              ON mp_other.match_id = m.id
             AND mp_other.seat != mp_self.seat
            WHERE mp_self.project_id = %s
              AND mp_self.project_version = %s

            UNION

            SELECT DISTINCT other_p.id, other_p.submitted_version
            FROM match_jobs mj
            CROSS JOIN LATERAL unnest(mj.project_ids) AS self_pid
            CROSS JOIN LATERAL unnest(mj.project_ids) AS other_pid
            JOIN projects other_p
              ON other_p.id = other_pid AND other_p.submitted_version > 0
            WHERE mj.mode_id = %s
              AND mj.status IN ('queued', 'running')
              AND self_pid  = %s
              AND other_pid <> %s
            """,
            (mode_id, ref.project_id, ref.version,
             mode_id, ref.project_id, ref.project_id),
        )
        return {VersionRef(project_id=pid, version=ver) for pid, ver in cur.fetchall()}


def pick_match_group(
    conn: psycopg.Connection,
    mode: Mode,
    underplayed: list[VersionStats],
    rng: random.Random | None = None,
) -> list[VersionRef] | None:
    """Pick one group of `mode.participant_count` versions for a multi mode.

    Greedy strategy:
      1. Seed the group with the most-underplayed version.
      2. Fill remaining seats from a candidate pool (all submitted versions
         except the seed and its already-played opponents). Walk candidates
         in least-played-with-seed order; break ties randomly.
      3. If the pool runs dry before the group is full, fall back to any
         other submitted version (rare — only when the pool of pairings is
         genuinely exhausted, e.g. only `n` submissions in total).

    Returns None when fewer than `mode.participant_count` submissions exist
    overall (the scheduler logs and moves on).

    Solo modes should not call this function — the scheduler enqueues solo
    jobs directly per underplayed version.
    """
    if mode.participant_count < 2:
        raise ValueError("pick_match_group is for multi modes only")
    if not underplayed:
        return None

    rng = rng or random.Random()

    # Full pool of submitted versions (we may need to fall back to non-
    # underplayed ones if the underplayed set is too small to fill the group).
    all_submitted = _list_all_submitted_versions(conn)
    if len(all_submitted) < mode.participant_count:
        return None

    seed = underplayed[0].ref
    played_against_seed = _list_played_opponents(conn, mode.id, seed)

    # Candidates: anyone except the seed.
    candidates = [v for v in all_submitted if v != seed]

    # Sort so unplayed-with-seed come first, then by overall underplay
    # (using the order in `underplayed` as a proxy — lower index = more
    # underplayed), then random tiebreak.
    underplay_rank = {vs.ref: i for i, vs in enumerate(underplayed)}
    def key(v: VersionRef) -> tuple[int, int, float]:
        unplayed = 0 if v not in played_against_seed else 1
        underplay = underplay_rank.get(v, len(underplayed))
        return (unplayed, underplay, rng.random())

    candidates.sort(key=key)

    group = [seed] + candidates[: mode.participant_count - 1]
    if len(group) < mode.participant_count:
        return None
    return group


def _list_all_submitted_versions(conn: psycopg.Connection) -> list[VersionRef]:
    """All currently-submitted (project_id, version) pairs."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, submitted_version FROM projects WHERE submitted_version > 0"
        )
        return [VersionRef(project_id=pid, version=ver) for pid, ver in cur.fetchall()]
