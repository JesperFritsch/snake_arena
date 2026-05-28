# services/sa_common/sa_common/db/matches.py
"""DB layer for matches and match_participants.

A match belongs to at most one mode (mode_id NULL = test match). The scorer
uses a lease (scoring_started_at + scored_at) so a transient bundler failure
doesn't permanently un-score a match. See docs/09_ranking_system.md.
"""
from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from sa_common.db.connection import get_conn
from sa_common.types import ParticipantRow, SimArgs
from sa_common.scoring import ParticipantScore

log = logging.getLogger(__name__)


MATCH_STATUSES = ("success", "failure")

# Scorer claim lease: how long after scoring_started_at without a scored_at
# before another worker can re-claim the row. Survives crashed workers.
SCORE_LEASE_INTERVAL = "5 minutes"

# Cap on retries — after this many transient failures, the scorer gives up
# and the row needs manual intervention. Prevents hot-loop on permanent
# failures (e.g. deleted bundle) in the event-driven (no-poll) daemon.
MAX_SCORING_ATTEMPTS = 3


@dataclass(slots=True)
class Match:
    id: int
    match_uuid: str
    status: str
    mode_id: int | None
    sim_args: dict[str, Any]
    started_at: datetime
    finished_at: datetime | None
    bundle_key: str | None
    error: str | None
    is_test: bool = False


def _row_to_match(row: dict[str, Any]) -> Match:
    return Match(
        id=row["id"],
        match_uuid=row["match_uuid"],
        status=row["status"],
        mode_id=row["mode_id"],
        sim_args=row["sim_args"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        bundle_key=row["bundle_key"],
        error=row["error"],
        is_test=row["is_test"],
    )


_MATCH_COLUMNS = """
    id, match_uuid, status, mode_id, sim_args,
    started_at, finished_at,
    bundle_key, error, is_test
"""


_PARTICIPANT_COLUMNS = """
    seat, project_id, project_version,
    final_length, fatal_step, survival_rank,
    killed_by_budget, metrics
"""


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------

def record_match_result(
    conn: psycopg.Connection,
    *,
    match_uuid: str,
    status: str,
    mode_id: int | None,
    sim_args: SimArgs,
    started_at: datetime,
    finished_at: datetime,
    bundle_key: str | None,
    error: str | None,
    participants: list[ParticipantRow],
    is_test: bool = False,
) -> int:
    """Persist a match and its participants. Returns matches.id."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO matches (
                match_uuid, status, mode_id, sim_args,
                started_at, finished_at,
                bundle_key, error, is_test
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                match_uuid,
                status,
                mode_id,
                Jsonb(sim_args.model_dump()),
                started_at,
                finished_at,
                bundle_key,
                error,
                is_test,
            ),
        )
        row = cur.fetchone()
        assert row is not None
        match_id = row[0]

        if participants:
            cur.executemany(
                """
                INSERT INTO match_participants (
                    match_id, seat, project_id, project_version,
                    final_length, fatal_step, survival_rank,
                    killed_by_budget, metrics
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        match_id,
                        p.seat,
                        p.project_id,
                        p.project_version,
                        p.final_length,
                        p.fatal_step,
                        p.survival_rank,
                        p.killed_by_budget,
                        Jsonb(p.metrics),
                    )
                    for p in participants
                ],
            )

    log.info("recorded match %s as id=%d", match_uuid, match_id)
    return match_id


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------

def get_match(conn: psycopg.Connection, match_id: int) -> Match | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"SELECT {_MATCH_COLUMNS} FROM matches WHERE id = %s",
            (match_id,),
        )
        row = cur.fetchone()
        return _row_to_match(row) if row else None


def get_match_by_uuid(conn: psycopg.Connection, match_uuid: str) -> Match | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"SELECT {_MATCH_COLUMNS} FROM matches WHERE match_uuid = %s",
            (match_uuid,),
        )
        row = cur.fetchone()
        return _row_to_match(row) if row else None


def list_matches(
    conn: psycopg.Connection,
    status: str | None = None,
    mode_id: int | None = None,
    is_test: bool | None = None,
    limit: int = 20,
) -> list[Match]:
    """List matches, newest first. Optional filters."""
    where_clauses: list[str] = []
    params: list[Any] = []
    if status is not None:
        where_clauses.append("status = %s")
        params.append(status)
    if mode_id is not None:
        where_clauses.append("mode_id = %s")
        params.append(mode_id)
    if is_test is not None:
        where_clauses.append("is_test = %s")
        params.append(is_test)
    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    params.append(limit)

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT {_MATCH_COLUMNS} FROM matches
            {where_sql}
            ORDER BY started_at DESC
            LIMIT %s
            """,
            tuple(params),
        )
        return [_row_to_match(row) for row in cur.fetchall()]


def get_match_participants(
    conn: psycopg.Connection, match_id: int,
) -> list[ParticipantRow]:
    """Return all participants of a match, ordered by seat."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT {_PARTICIPANT_COLUMNS}
            FROM match_participants
            WHERE match_id = %s
            ORDER BY seat ASC
            """,
            (match_id,),
        )
        return [ParticipantRow(**row) for row in cur.fetchall()]


# --------------------------------------------------------------------------
# Scorer lease — claim is revertable so transient failures don't drop matches.
# --------------------------------------------------------------------------

@dataclass(slots=True)
class ScoreClaim:
    match_id: int
    mode_id: int | None
    bundle_key: str


def claim_unscored_match(conn: psycopg.Connection) -> ScoreClaim | None:
    """Atomically lease one unscored *ranked* success match for scoring.

    Only ranked matches (mode_id IS NOT NULL) are eligible; test matches are
    scored synchronously by test_runner_daemon at run time so they don't
    race with bundle pruning.

    Picks up rows that are either unclaimed (scoring_started_at IS NULL) or
    whose lease has expired (older than SCORE_LEASE_INTERVAL).

    The caller MUST call release_score_lease() on failure, or mark_match_scored()
    on success. Returns None if nothing to score.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            WITH next AS (
                SELECT id
                FROM matches
                WHERE status = 'success'
                  AND mode_id IS NOT NULL
                  AND scored_at IS NULL
                  AND bundle_key IS NOT NULL
                  AND scoring_attempts < {MAX_SCORING_ATTEMPTS}
                  AND (scoring_started_at IS NULL
                       OR scoring_started_at < NOW() - INTERVAL '{SCORE_LEASE_INTERVAL}')
                ORDER BY finished_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            UPDATE matches
            SET scoring_started_at = NOW()
            FROM next
            WHERE matches.id = next.id
            RETURNING matches.id, matches.mode_id, matches.bundle_key
            """
        )
        row = cur.fetchone()
        if row is None:
            return None
        return ScoreClaim(
            match_id=row["id"],
            mode_id=row["mode_id"],
            bundle_key=row["bundle_key"],
        )


def release_score_lease(conn: psycopg.Connection, match_id: int) -> None:
    """Clear scoring_started_at and bump scoring_attempts.

    Called on transient failure (bundler error, etc.). Bumping attempts gives
    up after MAX_SCORING_ATTEMPTS so a permanent failure can't hot-loop the
    scorer — important now that the daemon is purely event-driven.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE matches "
            "SET scoring_started_at = NULL, "
            "    scoring_attempts = scoring_attempts + 1 "
            "WHERE id = %s AND scored_at IS NULL",
            (match_id,),
        )


def mark_match_scored(conn: psycopg.Connection, match_id: int) -> None:
    """Mark a match as successfully scored. Final terminal state."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE matches SET scored_at = NOW() WHERE id = %s",
            (match_id,),
        )


def reset_stale_score_leases(conn: psycopg.Connection) -> int:
    """Daemon startup hook: clear leases older than the lease interval.

    Returns the number of rows reset. Useful when a scorer worker died mid-job
    before the lease would have expired on its own.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE matches
            SET scoring_started_at = NULL
            WHERE scored_at IS NULL
              AND scoring_started_at IS NOT NULL
              AND scoring_started_at < NOW() - INTERVAL '{SCORE_LEASE_INTERVAL}'
            """
        )
        return cur.rowcount


def record_participant_scores(
    conn: psycopg.Connection,
    match_id: int,
    scores: list[ParticipantScore],
) -> None:
    """Write computed scores into match_participants.metrics."""
    with conn.cursor() as cur:
        for s in scores:
            cur.execute(
                """
                UPDATE match_participants
                SET metrics = %s
                WHERE match_id = %s AND seat = %s
                """,
                (Jsonb(s.to_metrics()), match_id, s.seat),
            )


# --------------------------------------------------------------------------
# Match-history reads (used by the leaderboard "click a project" view).
# --------------------------------------------------------------------------

@dataclass(slots=True)
class MatchParticipantSummary:
    seat: int
    project_id: int
    project_name: str
    final_length: int | None
    survival_rank: int | None
    metrics: dict[str, Any]


@dataclass(slots=True)
class MatchSummaryRow:
    id: int
    match_uuid: str
    status: str
    mode_id: int | None
    started_at: datetime
    finished_at: datetime | None
    bundle_key: str | None
    participants: list[MatchParticipantSummary]


def list_ranked_matches_for_project(
    conn: psycopg.Connection,
    project_id: int,
    limit: int = 20,
) -> list[MatchSummaryRow]:
    """Return the N most recent ranked successful matches the project played in.

    Each match includes all participants with their project names.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT
                m.id, m.match_uuid, m.status, m.mode_id,
                m.started_at, m.finished_at, m.bundle_key,
                mp.seat, mp.project_id, mp.final_length, mp.survival_rank, mp.metrics,
                p.name AS project_name
            FROM (
                SELECT m2.id
                FROM matches m2
                JOIN match_participants mp2
                    ON mp2.match_id = m2.id AND mp2.project_id = %s
                WHERE m2.is_test = FALSE
                  AND m2.status = 'success'
                ORDER BY m2.id DESC
                LIMIT %s
            ) sub
            JOIN matches m ON m.id = sub.id
            JOIN match_participants mp ON mp.match_id = m.id
            JOIN projects p ON p.id = mp.project_id
            ORDER BY m.started_at DESC, mp.seat ASC
            """,
            (project_id, limit),
        )
        matches: dict[int, MatchSummaryRow] = {}
        for row in cur.fetchall():
            mid = row["id"]
            if mid not in matches:
                matches[mid] = MatchSummaryRow(
                    id=mid,
                    match_uuid=row["match_uuid"],
                    status=row["status"],
                    mode_id=row["mode_id"],
                    started_at=row["started_at"],
                    finished_at=row["finished_at"],
                    bundle_key=row["bundle_key"],
                    participants=[],
                )
            matches[mid].participants.append(
                MatchParticipantSummary(
                    seat=row["seat"],
                    project_id=row["project_id"],
                    project_name=row["project_name"],
                    final_length=row["final_length"],
                    survival_rank=row["survival_rank"],
                    metrics=row["metrics"],
                )
            )
        return list(matches.values())


def count_matches_by_status(conn: psycopg.Connection) -> dict[str, int]:
    """Return {status: count} for all matches."""
    with conn.cursor() as cur:
        cur.execute("SELECT status, COUNT(*) FROM matches GROUP BY status")
        counts = {status: 0 for status in MATCH_STATUSES}
        for status, count in cur.fetchall():
            counts[status] = count
        return counts


# --------------------------------------------------------------------------
# CLI — for ops and manual testing
# --------------------------------------------------------------------------

def _format_match_line(match: Match) -> str:
    parts = [
        f"[{match.id:>4}]",
        f"{match.status:<7}",
        f"mode={match.mode_id if match.mode_id is not None else 'test':<5}",
        f"uuid={match.match_uuid}",
        f"started={match.started_at:%Y-%m-%d %H:%M:%S}",
    ]
    if match.finished_at:
        duration = (match.finished_at - match.started_at).total_seconds()
        parts.append(f"duration={duration:.1f}s")
    if match.error:
        truncated = match.error if len(match.error) <= 60 else match.error[:57] + "..."
        parts.append(f"error={truncated!r}")
    return "  ".join(parts)


def _format_participant_line(p: ParticipantRow) -> str:
    parts = [
        f"seat={p.seat}",
        f"project={p.project_id}.v{p.project_version}",
    ]
    if p.final_length is not None:
        parts.append(f"length={p.final_length}")
    if p.survival_rank is not None:
        parts.append(f"rank={p.survival_rank}")
    if p.fatal_step is not None:
        parts.append(f"step={p.fatal_step}")
    if p.killed_by_budget:
        parts.append("KILLED_BY_BUDGET")
    return "  ".join(parts)


def cli(argv) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    get_parser = subparsers.add_parser("get", help="show one match by id or uuid")
    get_id_group = get_parser.add_mutually_exclusive_group(required=True)
    get_id_group.add_argument("--id", type=int, dest="match_id")
    get_id_group.add_argument("--uuid", dest="match_uuid")

    list_parser = subparsers.add_parser("list", help="list recent matches")
    list_parser.add_argument("--status", choices=MATCH_STATUSES, default=None)
    list_parser.add_argument("--mode-id", type=int, default=None)
    list_parser.add_argument("--limit", type=int, default=20)

    subparsers.add_parser("counts", help="counts by status")

    parts_parser = subparsers.add_parser("participants", help="show participants of a match")
    parts_id_group = parts_parser.add_mutually_exclusive_group(required=True)
    parts_id_group.add_argument("--id", type=int, dest="match_id")
    parts_id_group.add_argument("--uuid", dest="match_uuid")

    return parser.parse_args(argv)


def _resolve_match(conn: psycopg.Connection, args: argparse.Namespace) -> Match:
    if args.match_id is not None:
        match = get_match(conn, args.match_id)
        ref = f"id={args.match_id}"
    else:
        match = get_match_by_uuid(conn, args.match_uuid)
        ref = f"uuid={args.match_uuid}"
    if match is None:
        raise SystemExit(f"match not found ({ref})")
    return match


def main():
    args = cli(sys.argv[1:])

    with get_conn(autocommit=False) as conn:
        with conn.transaction():
            if args.command == "get":
                match = _resolve_match(conn, args)
                result = _format_match_line(match)

            elif args.command == "list":
                matches = list_matches(
                    conn, status=args.status, mode_id=args.mode_id, limit=args.limit,
                )
                result = (
                    "\n".join(_format_match_line(m) for m in matches)
                    if matches else "(no matches)"
                )

            elif args.command == "counts":
                counts = count_matches_by_status(conn)
                total = sum(counts.values())
                lines = [f"{s:>9}: {c:>5}" for s, c in counts.items()]
                lines.append(f"{'total':>9}: {total:>5}")
                result = "\n".join(lines)

            elif args.command == "participants":
                match = _resolve_match(conn, args)
                rows = get_match_participants(conn, match.id)
                header = (
                    f"match {match.id} ({match.match_uuid}) — "
                    f"{match.status}, mode_id={match.mode_id}"
                )
                if not rows:
                    result = f"{header}\n(no participants)"
                else:
                    body = "\n".join(_format_participant_line(p) for p in rows)
                    result = f"{header}\n{body}"

            else:
                raise AssertionError("unreachable")

    print(result)


if __name__ == "__main__":
    main()
