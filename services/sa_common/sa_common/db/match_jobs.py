# services/sa_common/sa_common/db/match_jobs.py
"""Job queue operations.

Match jobs reference projects (not submissions) — the orchestrator resolves
each project's current submitted_image_tag and submitted_version at
dispatch time. This means that a user who enqueues many matches and then
re-submits between dispatches will see later matches use the newer version.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from sa_common.db.connection import get_conn
from sa_common.types import SimArgs

log = logging.getLogger(__name__)


JOB_STATUSES = ("queued", "running", "success", "failure", "cancelled")


@dataclass(slots=True)
class MatchJob:
    id: int
    status: str
    mode_id: int
    project_ids: list[int]
    sim_args: dict[str, Any]    # JSONB; reconstitute as SimArgs.model_validate() in callers
    requested_by: int | None
    requested_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    match_id: int | None
    error: str | None


def _row_to_job(row: dict[str, Any]) -> MatchJob:
    return MatchJob(
        id=row["id"],
        status=row["status"],
        mode_id=row["mode_id"],
        project_ids=row["project_ids"],
        sim_args=row["sim_args"],
        requested_by=row["requested_by"],
        requested_at=row["requested_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        match_id=row["match_id"],
        error=row["error"],
    )


_JOB_COLUMNS = """
    id, status, mode_id, project_ids, sim_args,
    requested_by, requested_at, started_at, finished_at,
    match_id, error
"""


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------

def enqueue_match_job(
    conn: psycopg.Connection,
    mode_id: int,
    project_ids: list[int],
    sim_args: SimArgs,
    requested_by: int | None = None,
) -> int:
    """Insert a new queued match job. Returns the new job's id."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO match_jobs (mode_id, project_ids, sim_args, requested_by)
            VALUES (%s, %s, %s, %s)
            RETURNING id
            """,
            (mode_id, project_ids, Jsonb(sim_args.model_dump()), requested_by),
        )
        row = cur.fetchone()
        assert row is not None
        job_id = row[0]

    log.info("queued match job id=%d (mode_id=%d)", job_id, mode_id)
    return job_id


def claim_one_queued_job(conn: psycopg.Connection) -> MatchJob | None:
    """Atomically claim the oldest queued job.

    Uses FOR UPDATE SKIP LOCKED so multiple orchestrators can safely
    compete for work without blocking each other.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            WITH next_job AS (
                SELECT id
                FROM match_jobs
                WHERE status = 'queued'
                ORDER BY requested_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            UPDATE match_jobs mj
            SET status = 'running',
                started_at = NOW()
            FROM next_job
            WHERE mj.id = next_job.id
            RETURNING
                mj.id, mj.status, mj.mode_id, mj.project_ids, mj.sim_args,
                mj.requested_by, mj.requested_at, mj.started_at,
                mj.finished_at, mj.match_id, mj.error
            """
        )
        row = cur.fetchone()
        if row is None:
            return None
        return _row_to_job(row)


def mark_job_success(
    conn: psycopg.Connection, job_id: int, match_id: int
) -> None:
    """Mark a running job as completed successfully."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE match_jobs
            SET status = 'success', finished_at = NOW(),
                match_id = %s, error = NULL
            WHERE id = %s
            """,
            (match_id, job_id),
        )


def mark_job_failure(
    conn: psycopg.Connection, job_id: int, error: str
) -> None:
    """Mark a running job as failed."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE match_jobs
            SET status = 'failure', finished_at = NOW(), error = %s
            WHERE id = %s
            """,
            (error, job_id),
        )


def cancel_job(conn: psycopg.Connection, job_id: int) -> bool:
    """Cancel a queued job.

    Returns True if a queued job was cancelled, False if it was already
    running/completed/missing.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE match_jobs
            SET status = 'cancelled', finished_at = NOW()
            WHERE id = %s AND status = 'queued'
            """,
            (job_id,),
        )
        return cur.rowcount > 0


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------

def get_job(conn: psycopg.Connection, job_id: int) -> MatchJob | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"SELECT {_JOB_COLUMNS} FROM match_jobs WHERE id = %s",
            (job_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return _row_to_job(row)


def list_jobs(
    conn: psycopg.Connection,
    status: str | None = None,
    limit: int = 20,
) -> list[MatchJob]:
    """List jobs, newest first. Optionally filter by status."""
    with conn.cursor(row_factory=dict_row) as cur:
        if status is not None:
            cur.execute(
                f"""
                SELECT {_JOB_COLUMNS} FROM match_jobs
                WHERE status = %s
                ORDER BY requested_at DESC
                LIMIT %s
                """,
                (status, limit),
            )
        else:
            cur.execute(
                f"""
                SELECT {_JOB_COLUMNS} FROM match_jobs
                ORDER BY requested_at DESC
                LIMIT %s
                """,
                (limit,),
            )
        return [_row_to_job(row) for row in cur.fetchall()]


def count_jobs_by_status(conn: psycopg.Connection) -> dict[str, int]:
    """Return {status: count} for all jobs. Useful for queue health checks."""
    with conn.cursor() as cur:
        cur.execute("SELECT status, COUNT(*) FROM match_jobs GROUP BY status")
        counts = {status: 0 for status in JOB_STATUSES}
        for status, count in cur.fetchall():
            counts[status] = count
        return counts


def count_queued_by_mode(conn: psycopg.Connection) -> dict[int, int]:
    """Return {mode_id: queued_count} for the scheduler's per-mode queue cap."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT mode_id, COUNT(*) FROM match_jobs "
            "WHERE status = 'queued' GROUP BY mode_id"
        )
        return {row[0]: row[1] for row in cur.fetchall()}


# --------------------------------------------------------------------------
# CLI — for ops and manual testing
# --------------------------------------------------------------------------

def _format_job_line(job: MatchJob) -> str:
    """One-line summary of a job for `list` output."""
    parts = [
        f"[{job.id:>4}]",
        f"{job.status:<9}",
        f"mode={job.mode_id}",
        f"projects={job.project_ids}",
        f"requested={job.requested_at:%Y-%m-%d %H:%M:%S}",
    ]
    if job.started_at:
        parts.append(f"started={job.started_at:%H:%M:%S}")
    if job.finished_at:
        parts.append(f"finished={job.finished_at:%H:%M:%S}")
    if job.match_id is not None:
        parts.append(f"match_id={job.match_id}")
    if job.error:
        truncated = job.error if len(job.error) <= 60 else job.error[:57] + "..."
        parts.append(f"error={truncated!r}")
    return "  ".join(parts)


def cli(argv) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    # enqueue
    enq = subparsers.add_parser("enqueue", help="add a new job to the queue")
    enq.add_argument(
        "project_ids", nargs="+", type=int,
        help="one or more project IDs to play (positional)",
    )
    enq.add_argument(
        "--mode-id", type=int, required=True,
        help="mode id this match belongs to (see `python -m sa_common.db.modes`)",
    )
    enq.add_argument(
        "--food", required=True,
    )
    enq.add_argument("--grid-width", type=int)
    enq.add_argument("--grid-height", type=int)
    enq.add_argument(
        "--map-name", default=None,
        help="snake_sim map id (omit for open grid)",
    )
    enq.add_argument(
        "--extra-sim-args", default=None,
        help="JSON dict merged into sim_args for any fields not covered "
             "by the flags above",
    )
    enq.add_argument(
        "--requested-by", type=int, default=None,
        help="user id who requested the job (optional)",
    )

    # get
    get_parser = subparsers.add_parser("get", help="show one job by id")
    get_parser.add_argument("job_id", type=int)

    # list
    list_parser = subparsers.add_parser("list", help="list recent jobs")
    list_parser.add_argument(
        "--status", choices=JOB_STATUSES, default=None,
        help="filter by status",
    )
    list_parser.add_argument(
        "--limit", type=int, default=20,
        help="max rows to show (default: 20)",
    )

    # counts
    subparsers.add_parser("counts", help="counts by status (queue health)")

    # cancel
    cancel_parser = subparsers.add_parser("cancel", help="cancel a queued job")
    cancel_parser.add_argument("job_id", type=int)

    return parser.parse_args(argv)


def _build_sim_args(args: argparse.Namespace) -> SimArgs:
    """Construct a SimArgs from CLI flags + optional --extra-sim-args JSON."""
    payload: dict[str, Any] = {
        "food": args.food,
        "map": args.map_name,
        "grid_width": args.grid_width,
        "grid_height": args.grid_height,
    }
    if args.extra_sim_args:
        try:
            extra = json.loads(args.extra_sim_args)
        except json.JSONDecodeError as e:
            raise SystemExit(f"--extra-sim-args is not valid JSON: {e}")
        if not isinstance(extra, dict):
            raise SystemExit("--extra-sim-args must be a JSON object")
        payload.update(extra)
    return SimArgs.model_validate(payload)


def main():
    args = cli(sys.argv[1:])

    with get_conn(autocommit=False) as conn:
        with conn.transaction():
            if args.command == "enqueue":
                sim_args = _build_sim_args(args)
                job_id = enqueue_match_job(
                    conn,
                    mode_id=args.mode_id,
                    project_ids=args.project_ids,
                    sim_args=sim_args,
                    requested_by=args.requested_by,
                )
                result = get_job(conn, job_id)

            elif args.command == "get":
                result = get_job(conn, args.job_id)
                if result is None:
                    raise SystemExit(f"job {args.job_id} not found")

            elif args.command == "list":
                jobs = list_jobs(conn, status=args.status, limit=args.limit)
                if not jobs:
                    result = "(no jobs)"
                else:
                    result = "\n".join(_format_job_line(j) for j in jobs)

            elif args.command == "counts":
                counts = count_jobs_by_status(conn)
                total = sum(counts.values())
                lines = [f"{status:>9}: {count:>5}" for status, count in counts.items()]
                lines.append(f"{'total':>9}: {total:>5}")
                result = "\n".join(lines)

            elif args.command == "cancel":
                cancelled = cancel_job(conn, args.job_id)
                if cancelled:
                    result = f"cancelled job {args.job_id}"
                else:
                    result = (
                        f"job {args.job_id} not cancelled "
                        "(already past queued, or doesn't exist)"
                    )

            else:
                raise AssertionError("unreachable")

    print(result)


if __name__ == "__main__":
    main()