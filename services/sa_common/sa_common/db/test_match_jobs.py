# services/sa_common/sa_common/db/test_match_jobs.py
"""DB layer for test_match_jobs.

Test matches are user-initiated dev-build runs. They differ from ranked
match_jobs in one key way: the player's slot uses dev_image_tag instead of
submitted_image_tag, while opponents still use their submitted images.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
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
    replay_json_path: str | None


_JOB_COLUMNS = """
    id, status, player_project_id, opponent_project_ids, sim_args,
    requested_by, requested_at, started_at, finished_at, match_id, error,
    replay_json_path
"""


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
        replay_json_path=row["replay_json_path"],
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
                j.finished_at, j.match_id, j.error, j.replay_json_path
            """
        )
        row = cur.fetchone()
        return _row_to_job(row) if row else None


def mark_test_job_success(
    conn: psycopg.Connection,
    job_id: int,
    match_id: int,
    replay_json_path: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE test_match_jobs
            SET status = 'success', finished_at = NOW(), match_id = %s,
                replay_json_path = %s, error = NULL
            WHERE id = %s
            """,
            (match_id, replay_json_path, job_id),
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


def get_test_job(conn: psycopg.Connection, job_id: int) -> TestMatchJob | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"SELECT {_JOB_COLUMNS} FROM test_match_jobs WHERE id = %s",
            (job_id,),
        )
        row = cur.fetchone()
        return _row_to_job(row) if row else None
