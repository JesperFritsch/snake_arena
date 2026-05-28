# services/sa_common/sa_common/db/test_match_jobs.py
"""DB layer for test_match_jobs.

Test matches are user-initiated dev-build runs. They differ from ranked
match_jobs in one key way: the player's slot uses dev_image_tag instead of
submitted_image_tag, while opponents still use their submitted images.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from sa_common.types import SimArgs

log = logging.getLogger(__name__)


@dataclass(slots=True)
class TestMatchJob:
    id: int
    status: str
    player_project_id: int
    opponent_project_ids: list[int]
    sim_args: dict[str, Any]
    requested_by: int | None
    requested_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    match_id: int | None
    error: str | None
    bundle_key: str | None
    pinned: bool
    # Populated by the API layer (join with projects); not stored in this table.
    participant_names: list[str] = field(default_factory=list)
    # Computed via ROW_NUMBER window function; None when not needed.
    match_number: int | None = None


_JOB_COLUMNS = """
    id, status, player_project_id, opponent_project_ids, sim_args,
    requested_by, requested_at, started_at, finished_at, match_id, error,
    bundle_key, pinned
"""

# Columns for the CTE-based queries that also compute match_number.
_JOB_COLUMNS_WITH_NUM = _JOB_COLUMNS + ", match_number"


def _row_to_job(row: dict[str, Any]) -> TestMatchJob:
    return TestMatchJob(
        id=row["id"],
        status=row["status"],
        player_project_id=row["player_project_id"],
        opponent_project_ids=row["opponent_project_ids"],
        sim_args=row["sim_args"],
        requested_by=row["requested_by"],
        requested_at=row["requested_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        match_id=row["match_id"],
        error=row["error"],
        bundle_key=row["bundle_key"],
        pinned=row["pinned"],
        match_number=row.get("match_number"),
    )


def enqueue_test_match_job(
    conn: psycopg.Connection,
    player_project_id: int,
    opponent_project_ids: list[int],
    sim_args: SimArgs,
    requested_by: int | None = None,
) -> int:
    """Insert a queued test match job. Returns the new job's id."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO test_match_jobs
                (player_project_id, opponent_project_ids, sim_args, requested_by)
            VALUES (%s, %s, %s, %s)
            RETURNING id
            """,
            (player_project_id, opponent_project_ids, Jsonb(sim_args.model_dump()), requested_by),
        )
        row = cur.fetchone()
        assert row is not None
        job_id = row[0]
    log.info("queued test match job id=%d", job_id)
    return job_id


def claim_one_queued_test_job(conn: psycopg.Connection) -> TestMatchJob | None:
    """Atomically claim the oldest queued test match job (SKIP LOCKED)."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            WITH next_job AS (
                SELECT id FROM test_match_jobs
                WHERE status = 'queued'
                ORDER BY requested_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            UPDATE test_match_jobs j
            SET status = 'running', started_at = NOW()
            FROM next_job
            WHERE j.id = next_job.id
            RETURNING
                j.id, j.status, j.player_project_id, j.opponent_project_ids,
                j.sim_args, j.requested_by, j.requested_at, j.started_at,
                j.finished_at, j.match_id, j.error, j.bundle_key, j.pinned
            """
        )
        row = cur.fetchone()
        return _row_to_job(row) if row else None


def mark_test_job_success(
    conn: psycopg.Connection,
    job_id: int,
    match_id: int,
    bundle_key: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE test_match_jobs
            SET status = 'success', finished_at = NOW(), match_id = %s,
                bundle_key = %s, error = NULL
            WHERE id = %s
            """,
            (match_id, bundle_key, job_id),
        )


def mark_test_job_failure(conn: psycopg.Connection, job_id: int, error: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE test_match_jobs
            SET status = 'failure', finished_at = NOW(), error = %s
            WHERE id = %s
            """,
            (error, job_id),
        )


def list_test_jobs_for_project(
    conn: psycopg.Connection,
    player_project_id: int,
    limit: int = 10,
) -> list[TestMatchJob]:
    """Return pinned jobs (all of them) plus the most recent `limit` unpinned
    jobs for the project, ordered pinned-first then by recency.

    Each job carries a project-relative `match_number` computed from
    chronological order (1 = oldest).
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            WITH project_jobs AS (
                SELECT id, pinned, requested_at
                FROM test_match_jobs
                WHERE player_project_id = %s
            ),
            ranked AS (
                SELECT id,
                       ROW_NUMBER() OVER (ORDER BY requested_at ASC, id ASC) AS match_number
                FROM project_jobs
            ),
            recent_unpinned AS (
                SELECT id FROM project_jobs
                WHERE pinned = FALSE
                ORDER BY requested_at DESC, id DESC
                LIMIT %s
            )
            SELECT j.{_JOB_COLUMNS}, r.match_number
            FROM test_match_jobs j
            JOIN ranked r ON r.id = j.id
            WHERE j.player_project_id = %s
              AND j.status IN ('success', 'failure', 'cancelled')
              AND (j.pinned = TRUE OR j.id IN (SELECT id FROM recent_unpinned))
            ORDER BY j.pinned DESC, j.requested_at DESC, j.id DESC
            """,
            (player_project_id, limit, player_project_id),
        )
        return [_row_to_job(row) for row in cur.fetchall()]


def get_test_job(conn: psycopg.Connection, job_id: int) -> TestMatchJob | None:
    """Fetch a single job by id, with its project-relative match_number."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            WITH ranked AS (
                SELECT id,
                       ROW_NUMBER() OVER (ORDER BY requested_at ASC, id ASC) AS match_number
                FROM test_match_jobs
                WHERE player_project_id = (
                    SELECT player_project_id FROM test_match_jobs WHERE id = %s
                )
            )
            SELECT j.{_JOB_COLUMNS}, r.match_number
            FROM test_match_jobs j
            JOIN ranked r ON r.id = j.id
            WHERE j.id = %s
            """,
            (job_id, job_id),
        )
        row = cur.fetchone()
        return _row_to_job(row) if row else None


def set_pinned(conn: psycopg.Connection, job_id: int, pinned: bool) -> None:
    """Pin or unpin a test match job."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE test_match_jobs SET pinned = %s WHERE id = %s",
            (pinned, job_id),
        )


def count_pinned_test_jobs(conn: psycopg.Connection, player_project_id: int) -> int:
    """Count pinned jobs for a project."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM test_match_jobs WHERE player_project_id = %s AND pinned = TRUE",
            (player_project_id,),
        )
        row = cur.fetchone()
        return row[0] if row else 0


def get_bundle_keys_for_project(
    conn: psycopg.Connection, player_project_id: int
) -> list[str]:
    """Return all non-null bundle_keys for a project (used before project delete)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT bundle_key FROM test_match_jobs WHERE player_project_id = %s AND bundle_key IS NOT NULL",
            (player_project_id,),
        )
        return [row[0] for row in cur.fetchall()]


def cancel_queued_test_job(conn: psycopg.Connection, job_id: int) -> bool:
    """Cancel a queued job. Returns True if it was queued and is now cancelled."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE test_match_jobs
            SET status = 'cancelled', finished_at = NOW()
            WHERE id = %s AND status = 'queued'
            """,
            (job_id,),
        )
        return cur.rowcount > 0


def count_active_test_jobs_for_project(
    conn: psycopg.Connection, player_project_id: int
) -> int:
    """Count queued or running test jobs for a project."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM test_match_jobs WHERE player_project_id = %s AND status IN ('queued', 'running')",
            (player_project_id,),
        )
        row = cur.fetchone()
        return row[0] if row else 0


# A 'running' test-match job whose started_at is older than this is assumed
# to belong to a crashed orchestrator and gets failed by the next worker that
# starts up. The threshold has to be longer than the worst legitimate runtime
# (build + sim + analyze + bundle write) so a scale-up of test-runners can't
# fail an in-flight job that another worker is still processing.
STALE_RUNNING_INTERVAL = "15 minutes"


def reset_stale_running_jobs(conn: psycopg.Connection) -> int:
    """Reset jobs stuck in 'running' past STALE_RUNNING_INTERVAL to 'failure'.

    Called at daemon startup. The time gate makes this safe to run with
    multiple test-runners — a freshly-started worker won't fail an in-flight
    job owned by a peer.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE test_match_jobs
            SET status = 'failure', finished_at = NOW(),
                error = 'orchestrator restarted while job was running'
            WHERE status = 'running'
              AND started_at < NOW() - INTERVAL '{STALE_RUNNING_INTERVAL}'
            """,
        )
        count = cur.rowcount
    if count:
        log.warning("reset %d stale running test jobs to failure", count)
    return count


_BUNDLE_LIMIT = 10


def prune_unpinned_test_jobs(
    conn: psycopg.Connection,
    player_project_id: int,
) -> list[str]:
    """Delete terminal unpinned jobs so that total stored bundles stays at or
    below _BUNDLE_LIMIT (pinned + unpinned combined).

    Only deletes jobs with a terminal status (success/failure/cancelled) so
    active (queued/running) jobs are never removed. Returns the bundle_keys of
    deleted rows (non-null only) so the caller can purge storage too.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM test_match_jobs
            WHERE id IN (
                SELECT id FROM test_match_jobs
                WHERE player_project_id = %s
                  AND pinned = FALSE
                  AND status IN ('success', 'failure', 'cancelled')
                ORDER BY requested_at DESC, id DESC
                OFFSET GREATEST(0, %s - (
                    SELECT COUNT(*) FROM test_match_jobs
                    WHERE player_project_id = %s AND pinned = TRUE
                ))
            )
            RETURNING bundle_key
            """,
            (player_project_id, _BUNDLE_LIMIT, player_project_id),
        )
        keys = [row[0] for row in cur.fetchall() if row[0] is not None]
    if keys:
        log.info(
            "pruned %d unpinned test jobs for project %d", len(keys), player_project_id
        )
    return keys
