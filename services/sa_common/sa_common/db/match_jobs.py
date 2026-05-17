# sa_common/db/match_jobs.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row


@dataclass(slots=True)
class MatchJob:
    id: int
    status: str
    submission_ids: list[int]
    sim_args: dict[str, Any]
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
        submission_ids=row["submission_ids"],
        sim_args=row["sim_args"],
        requested_by=row["requested_by"],
        requested_at=row["requested_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        match_id=row["match_id"],
        error=row["error"],
    )


def enqueue_match_job(
    conn: psycopg.Connection,
    submission_ids: list[int],
    sim_args: dict[str, Any],
    requested_by: int | None = None,
) -> int:
    """
    Insert a new queued match job.

    Returns:
        Newly created job ID.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO match_jobs (
                submission_ids,
                sim_args,
                requested_by
            )
            VALUES (%s, %s, %s)
            RETURNING id
            """,
            (
                submission_ids,
                sim_args,
                requested_by,
            ),
        )

        row = cur.fetchone()
        assert row is not None
        return row[0]


def claim_one_queued_job(conn: psycopg.Connection) -> MatchJob | None:
    """
    Atomically claim the oldest queued job.

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
            SET
                status = 'running',
                started_at = NOW()
            FROM next_job
            WHERE mj.id = next_job.id
            RETURNING
                mj.id,
                mj.status,
                mj.submission_ids,
                mj.sim_args,
                mj.requested_by,
                mj.requested_at,
                mj.started_at,
                mj.finished_at,
                mj.match_id,
                mj.error
            """
        )

        row = cur.fetchone()
        if row is None:
            return None

        return _row_to_job(row)


def mark_job_success(
    conn: psycopg.Connection,
    job_id: int,
    match_id: int,
) -> None:
    """
    Mark a running job as completed successfully.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE match_jobs
            SET
                status = 'success',
                finished_at = NOW(),
                match_id = %s,
                error = NULL
            WHERE id = %s
            """,
            (
                match_id,
                job_id,
            ),
        )


def mark_job_failure(
    conn: psycopg.Connection,
    job_id: int,
    error: str,
) -> None:
    """
    Mark a running job as failed.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE match_jobs
            SET
                status = 'failure',
                finished_at = NOW(),
                error = %s
            WHERE id = %s
            """,
            (
                error,
                job_id,
            ),
        )


def cancel_job(
    conn: psycopg.Connection,
    job_id: int,
) -> bool:
    """
    Cancel a queued job.

    Returns:
        True if a queued job was cancelled.
        False if the job was already running/completed/missing.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE match_jobs
            SET
                status = 'cancelled',
                finished_at = NOW()
            WHERE
                id = %s
                AND status = 'queued'
            """,
            (job_id,),
        )

        return cur.rowcount > 0


def get_job(
    conn: psycopg.Connection,
    job_id: int,
) -> MatchJob | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT
                id,
                status,
                submission_ids,
                sim_args,
                requested_by,
                requested_at,
                started_at,
                finished_at,
                match_id,
                error
            FROM match_jobs
            WHERE id = %s
            """,
            (job_id,),
        )

        row = cur.fetchone()
        if row is None:
            return None

        return _row_to_job(row)
