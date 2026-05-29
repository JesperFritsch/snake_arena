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
    """Return submitted versions that haven't hit the mode's target.

    Both solo and multi modes count *matches played + in-flight* (success
    matches plus queued/running jobs) against `mode.target`. The same
    metric drives the leaderboard's eligibility check, so the matchmaker
    and the leaderboard agree on "played enough."

    Variety (distinct opponents played) is a sort-order preference inside
    pick_match_group, not a hard stop — once a project has played every
    other project at least once, additional matches will repeat pairings,
    and that's fine.

    Counting in-flight match_jobs (queued/running) prevents the scheduler
    from filling its per-mode queue cap with five copies of the same
    pairing in a single tick.

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
    """Multi modes: each version needs `mode.target` matches played + in-flight.

    matches_played counts the project's appearances in success matches plus
    queued/running jobs for this mode. distinct_opponents is returned in
    the stats but is NOT used as the stop condition — it drives pick_match_group's
    "prefer fresh pairings" sort order. Once everyone has played everyone,
    pairings repeat, and that's fine: target is about volume of matches.
    """
    n_submitted = _count_submitted(conn)
    if n_submitted < mode.participant_count:
        return []
    target = mode.target_matches_per_version

    with conn.cursor() as cur:
        cur.execute(
            """
            WITH submitted AS (
                SELECT id AS project_id, submitted_version AS version, submitted_at
                FROM projects
                WHERE submitted_version > 0
            ),
            -- Match appearances (success + in-flight). Each row = one
            -- match this version participated in (or is about to).
            played_success_matches AS (
                SELECT mp.project_id, mp.project_version, mp.match_id
                FROM match_participants mp
                JOIN matches m
                  ON m.id = mp.match_id
                 AND m.mode_id = %(mode_id)s
                 AND m.status = 'success'
            ),
            in_flight_matches AS (
                -- One row per (project, job) for queued/running jobs.
                SELECT self_p.id AS project_id,
                       self_p.submitted_version AS project_version,
                       mj.id AS match_id
                FROM match_jobs mj
                CROSS JOIN LATERAL unnest(mj.project_ids) AS self_pid
                JOIN projects self_p
                  ON self_p.id = self_pid AND self_p.submitted_version > 0
                WHERE mj.mode_id = %(mode_id)s
                  AND mj.status IN ('queued', 'running')
            ),
            -- Distinct opponent (project, version) pairs from BOTH sources.
            -- The two CTEs share no match_id space (one is from matches,
            -- the other from match_jobs), but UNION dedupes per opponent
            -- pair so distinct_opponents counts each pairing once.
            opponent_pairs AS (
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

                UNION

                SELECT self_p.id, self_p.submitted_version,
                       other_p.id, other_p.submitted_version
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
            match_counts AS (
                SELECT project_id, project_version, COUNT(*) AS matches_played
                FROM (
                    SELECT * FROM played_success_matches
                    UNION ALL
                    SELECT * FROM in_flight_matches
                ) AS all_matches
                GROUP BY project_id, project_version
            ),
            opp_counts AS (
                SELECT project_id, project_version,
                       COUNT(DISTINCT (opp_project, opp_version)) AS distinct_opponents
                FROM opponent_pairs
                GROUP BY project_id, project_version
            )
            SELECT s.project_id,
                   s.version,
                   COALESCE(mc.matches_played, 0)      AS matches_played,
                   COALESCE(oc.distinct_opponents, 0)  AS distinct_opponents
            FROM submitted s
            LEFT JOIN match_counts mc
                   ON mc.project_id = s.project_id
                  AND mc.project_version = s.version
            LEFT JOIN opp_counts oc
                   ON oc.project_id = s.project_id
                  AND oc.project_version = s.version
            WHERE COALESCE(mc.matches_played, 0) < %(target)s
            ORDER BY matches_played    ASC,
                     distinct_opponents ASC,
                     s.submitted_at    DESC
            """,
            {"mode_id": mode.id, "target": target},
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
      1. Seed the group with the most-underplayed version (lowest matches_played).
      2. Fill the remaining seats from all submitted versions, sorted so:
           a) opponents the seed hasn't played yet come first (variety),
           b) then by underplay rank (let the underplayed catch up),
           c) random tiebreak.
         Once every other project has played the seed at least once, the
         "unplayed" tier is empty and the sort falls through to underplay
         rank — i.e. pairings repeat. That's by design: matches_played is
         the target, distinct opponents is a soft preference.

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
