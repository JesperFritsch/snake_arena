# services/sa_common/sa_common/db/matches.py
"""DB layer for matches and match_participants.

This module is pure storage: it accepts data already shaped for the schema
and writes it. Any transformation from raw MatchResult/SimAnalysis to
participant rows lives outside this module (see runner.match_results).

Note: enqueue_match_job and other queue operations live in match_jobs.py.
"""
from __future__ import annotations

import logging
from datetime import datetime

import psycopg
from psycopg.types.json import Jsonb

from sa_common.types import ParticipantRow, SimArgs

log = logging.getLogger(__name__)


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
) -> int:
    """Persist a match and its participants.

    Caller is responsible for shaping `participants` correctly — this
    function does no inference, no rank computation, no name lookups.

    `mode` is denormalized into a typed column on matches AND lives inside
    the sim_args JSONB; both come from sim_args.mode so they can't drift.

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
                replay_r2_key, error
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
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