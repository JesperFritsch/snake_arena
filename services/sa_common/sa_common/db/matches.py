# services/sa_common/sa_common/db/matches.py
"""DB layer for matches and match_participants.

A match belongs to at most one mode (mode_id NULL = test match). There is
no scorer step: aggregate scores are computed on demand by
sa_common.db.agent_scores.compute_mode_scores, so a match counts toward
the leaderboard the moment record_match_result commits.
See docs/09_ranking_system.md.
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


_PARTICIPANT_COLUMNS = """
    seat, project_id, project_version,
    final_length, fatal_step, survival_rank,
    killed_by_budget, metrics
"""


_MATCH_COLUMNS = """
    id, match_uuid, status, mode_id, sim_args,
    started_at, finished_at,
    bundle_key, error, is_test
"""


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
    mode_ids: list[int] | None = None,
) -> list[MatchSummaryRow]:
    """Return the N most recent ranked successful matches the project played in.

    Each match includes all participants with their project names. If
    `mode_ids` is given, only matches from those modes are returned — used
    when the leaderboard modal opens scoped to a per-mode or per-group tab
    so the counts match the leaderboard row.
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
                  AND (%s::bigint[] IS NULL OR m2.mode_id = ANY(%s::bigint[]))
                ORDER BY m2.id DESC
                LIMIT %s
            ) sub
            JOIN matches m ON m.id = sub.id
            JOIN match_participants mp ON mp.match_id = m.id
            JOIN projects p ON p.id = mp.project_id
            ORDER BY m.started_at DESC, mp.seat ASC
            """,
            (project_id, mode_ids, mode_ids, limit),
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
