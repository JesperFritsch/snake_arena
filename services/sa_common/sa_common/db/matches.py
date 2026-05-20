# services/sa_common/sa_common/db/matches.py
"""DB layer for matches and match_participants — writes and reads.

record_match_result accepts data already shaped for the schema and writes
it; any transformation from raw MatchResult/SimAnalysis to participant
rows lives outside this module (see runner.match_results).

Note: enqueue_match_job and other queue operations live in match_jobs.py.
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

log = logging.getLogger(__name__)


MATCH_STATUSES = ("success", "failure")
MATCH_MODES = ("multiplayer", "solo")


@dataclass(slots=True)
class Match:
    id: int
    match_uuid: str
    status: str    # 'success' | 'failure'
    mode: str      # 'multiplayer' | 'solo'
    sim_args: dict[str, Any]    # JSONB; reconstitute as SimArgs.model_validate() in callers
    started_at: datetime
    finished_at: datetime | None
    replay_r2_key: str | None
    error: str | None
    is_test: bool = False


def _row_to_match(row: dict[str, Any]) -> Match:
    return Match(
        id=row["id"],
        match_uuid=row["match_uuid"],
        status=row["status"],
        mode=row["mode"],
        sim_args=row["sim_args"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        replay_r2_key=row["replay_r2_key"],
        error=row["error"],
        is_test=row["is_test"],
    )


_MATCH_COLUMNS = """
    id, match_uuid, status, mode, sim_args,
    started_at, finished_at,
    replay_r2_key, error, is_test
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
    sim_args: SimArgs,
    started_at: datetime,
    finished_at: datetime,
    replay_r2_key: str | None,
    error: str | None,
    participants: list[ParticipantRow],
    is_test: bool = False,
) -> int:
    """Persist a match and its participants.

    Caller is responsible for shaping `participants` correctly — this
    function does no inference, no rank computation, no name lookups.

    Each participant's (project_id, project_version) is the durable
    reference to "which version of which agent played." submitted_version
    is captured at dispatch time, so a project being re-submitted between
    dispatch and recording does not affect the recorded version.

    Returns:
        matches.id
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO matches (
                match_uuid, status, mode, sim_args,
                started_at, finished_at,
                replay_r2_key, error, is_test
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                match_uuid,
                status,
                "multiplayer" if len(participants) > 1 else "solo",
                Jsonb(sim_args.model_dump()),
                started_at,
                finished_at,
                replay_r2_key,
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
        if row is None:
            return None
        return _row_to_match(row)


def get_match_by_uuid(conn: psycopg.Connection, match_uuid: str) -> Match | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"SELECT {_MATCH_COLUMNS} FROM matches WHERE match_uuid = %s",
            (match_uuid,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return _row_to_match(row)


def list_matches(
    conn: psycopg.Connection,
    status: str | None = None,
    mode: str | None = None,
    is_test: bool | None = None,
    limit: int = 20,
) -> list[Match]:
    """List matches, newest first. Optionally filter by status, mode, and/or is_test."""
    where_clauses: list[str] = []
    params: list[Any] = []
    if status is not None:
        where_clauses.append("status = %s")
        params.append(status)
    if mode is not None:
        where_clauses.append("mode = %s")
        params.append(mode)
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
    conn: psycopg.Connection, match_id: int
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
    """One-line summary of a match for `list` output."""
    parts = [
        f"[{match.id:>4}]",
        f"{match.status:<7}",
        f"{match.mode:<11}",
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
    """One-line summary of a participant for `participants` output."""
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

    # get
    get_parser = subparsers.add_parser("get", help="show one match by id or uuid")
    get_id_group = get_parser.add_mutually_exclusive_group(required=True)
    get_id_group.add_argument("--id", type=int, dest="match_id")
    get_id_group.add_argument("--uuid", dest="match_uuid")

    # list
    list_parser = subparsers.add_parser("list", help="list recent matches")
    list_parser.add_argument(
        "--status", choices=MATCH_STATUSES, default=None,
        help="filter by status",
    )
    list_parser.add_argument(
        "--mode", choices=MATCH_MODES, default=None,
        help="filter by mode",
    )
    list_parser.add_argument(
        "--limit", type=int, default=20,
        help="max rows to show (default: 20)",
    )

    # counts
    subparsers.add_parser("counts", help="counts by status")

    # participants
    parts_parser = subparsers.add_parser(
        "participants", help="show participants of a match"
    )
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
                result = _resolve_match(conn, args)

            elif args.command == "list":
                matches = list_matches(
                    conn, status=args.status, mode=args.mode, limit=args.limit
                )
                if not matches:
                    result = "(no matches)"
                else:
                    result = "\n".join(_format_match_line(m) for m in matches)

            elif args.command == "counts":
                counts = count_matches_by_status(conn)
                total = sum(counts.values())
                lines = [f"{status:>9}: {count:>5}" for status, count in counts.items()]
                lines.append(f"{'total':>9}: {total:>5}")
                result = "\n".join(lines)

            elif args.command == "participants":
                match = _resolve_match(conn, args)
                rows = get_match_participants(conn, match.id)
                header = (
                    f"match {match.id} ({match.match_uuid}) — "
                    f"{match.status}, {match.mode}"
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