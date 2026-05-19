# services/sa_common/sa_common/db/build_jobs.py
"""Build job queue operations.

Build jobs reference projects. The builder daemon reads the project's
current dev_code_archive at claim time and builds it into dev_image_tag,
leaving dev_build_status as 'ready' or 'failed' regardless of outcome.
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

from sa_common.db.connection import get_conn

log = logging.getLogger(__name__)


JOB_STATUSES = ("queued", "running", "success", "failure", "cancelled")


@dataclass(slots=True)
class BuildJob:
    id: int
    project_id: int
    status: str
    requested_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None


def _row_to_job(row: dict[str, Any]) -> BuildJob:
    return BuildJob(
        id=row["id"],
        project_id=row["project_id"],
        status=row["status"],
        requested_at=row["requested_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        error=row["error"],
    )


_JOB_COLUMNS = """
    id, project_id, status, requested_at,
    started_at, finished_at, error
"""


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------

def enqueue_build_job(conn: psycopg.Connection, project_id: int) -> int:
    """Insert a new queued build job and flip dev_build_status to 'building'
    for immediate UI feedback. Returns the new job's id."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO build_jobs (project_id) VALUES (%s)
            RETURNING id
            """,
            (project_id,),
        )
        row = cur.fetchone()
        assert row is not None
        job_id = row[0]
        cur.execute(
            "UPDATE projects SET dev_build_status = 'building' WHERE id = %s",
            (project_id,),
        )

    log.info("queued build job id=%d project_id=%d", job_id, project_id)
    return job_id


def claim_one_queued_build_job(conn: psycopg.Connection) -> BuildJob | None:
    """Atomically claim the oldest queued build job.

    Uses FOR UPDATE SKIP LOCKED so multiple builder daemons can safely
    compete for work without blocking each other.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            WITH next_job AS (
                SELECT id
                FROM build_jobs
                WHERE status = 'queued'
                ORDER BY requested_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            UPDATE build_jobs bj
            SET status = 'running',
                started_at = NOW()
            FROM next_job
            WHERE bj.id = next_job.id
            RETURNING
                bj.id, bj.project_id, bj.status, bj.requested_at,
                bj.started_at, bj.finished_at, bj.error
            """
        )
        row = cur.fetchone()
        if row is None:
            return None
        return _row_to_job(row)


def mark_build_job_success(conn: psycopg.Connection, job_id: int) -> None:
    """Mark a running build job as completed successfully."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE build_jobs
            SET status = 'success', finished_at = NOW(), error = NULL
            WHERE id = %s
            """,
            (job_id,),
        )


def mark_build_job_failure(
    conn: psycopg.Connection, job_id: int, error: str
) -> None:
    """Mark a running build job as failed."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE build_jobs
            SET status = 'failure', finished_at = NOW(), error = %s
            WHERE id = %s
            """,
            (error, job_id),
        )


def cancel_build_job(conn: psycopg.Connection, job_id: int) -> bool:
    """Cancel a queued build job.

    Returns True if a queued job was cancelled, False if it was already
    running/completed/missing. Does not touch projects.dev_build_status;
    the next enqueue or build outcome resets it.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE build_jobs
            SET status = 'cancelled', finished_at = NOW()
            WHERE id = %s AND status = 'queued'
            """,
            (job_id,),
        )
        return cur.rowcount > 0


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------

def get_build_job(conn: psycopg.Connection, job_id: int) -> BuildJob | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"SELECT {_JOB_COLUMNS} FROM build_jobs WHERE id = %s",
            (job_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return _row_to_job(row)


def list_build_jobs(
    conn: psycopg.Connection,
    status: str | None = None,
    limit: int = 20,
) -> list[BuildJob]:
    """List build jobs, newest first. Optionally filter by status."""
    with conn.cursor(row_factory=dict_row) as cur:
        if status is not None:
            cur.execute(
                f"""
                SELECT {_JOB_COLUMNS} FROM build_jobs
                WHERE status = %s
                ORDER BY requested_at DESC
                LIMIT %s
                """,
                (status, limit),
            )
        else:
            cur.execute(
                f"""
                SELECT {_JOB_COLUMNS} FROM build_jobs
                ORDER BY requested_at DESC
                LIMIT %s
                """,
                (limit,),
            )
        return [_row_to_job(row) for row in cur.fetchall()]


def count_build_jobs_by_status(conn: psycopg.Connection) -> dict[str, int]:
    """Return {status: count} for all build jobs. Useful for queue health."""
    with conn.cursor() as cur:
        cur.execute("SELECT status, COUNT(*) FROM build_jobs GROUP BY status")
        counts = {status: 0 for status in JOB_STATUSES}
        for status, count in cur.fetchall():
            counts[status] = count
        return counts


# --------------------------------------------------------------------------
# CLI — for ops and manual testing
# --------------------------------------------------------------------------

def _format_job_line(job: BuildJob) -> str:
    """One-line summary of a job for `list` output."""
    parts = [
        f"[{job.id:>4}]",
        f"{job.status:<9}",
        f"project_id={job.project_id}",
        f"requested={job.requested_at:%Y-%m-%d %H:%M:%S}",
    ]
    if job.started_at:
        parts.append(f"started={job.started_at:%H:%M:%S}")
    if job.finished_at:
        parts.append(f"finished={job.finished_at:%H:%M:%S}")
    if job.error:
        truncated = job.error if len(job.error) <= 60 else job.error[:57] + "..."
        parts.append(f"error={truncated!r}")
    return "  ".join(parts)


def cli(argv) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    # enqueue
    enq = subparsers.add_parser("enqueue", help="queue a build for a project")
    enq.add_argument("project_id", type=int, help="project to build (positional)")

    # get
    get_parser = subparsers.add_parser("get", help="show one build job by id")
    get_parser.add_argument("job_id", type=int)

    # list
    list_parser = subparsers.add_parser("list", help="list recent build jobs")
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
    cancel_parser = subparsers.add_parser("cancel", help="cancel a queued build job")
    cancel_parser.add_argument("job_id", type=int)

    return parser.parse_args(argv)


def main():
    args = cli(sys.argv[1:])

    with get_conn(autocommit=False) as conn:
        with conn.transaction():
            if args.command == "enqueue":
                job_id = enqueue_build_job(conn, args.project_id)
                result = get_build_job(conn, job_id)

            elif args.command == "get":
                result = get_build_job(conn, args.job_id)
                if result is None:
                    raise SystemExit(f"job {args.job_id} not found")

            elif args.command == "list":
                jobs = list_build_jobs(conn, status=args.status, limit=args.limit)
                if not jobs:
                    result = "(no jobs)"
                else:
                    result = "\n".join(_format_job_line(j) for j in jobs)

            elif args.command == "counts":
                counts = count_build_jobs_by_status(conn)
                total = sum(counts.values())
                lines = [f"{status:>9}: {count:>5}" for status, count in counts.items()]
                lines.append(f"{'total':>9}: {total:>5}")
                result = "\n".join(lines)

            elif args.command == "cancel":
                cancelled = cancel_build_job(conn, args.job_id)
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