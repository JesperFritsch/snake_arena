# services/orchestrator/orchestrator/scheduler_daemon.py
"""Daemon that schedules ranked matches.

Event-driven: walks every enabled mode, enqueues underplayed jobs up to the
per-mode queue cap, then blocks on a wakeup Event fed by a Postgres LISTEN
on `scheduler_wakeup`. Triggers fire that channel when:

  - a project is (re-)submitted
  - a mode is added or enabled
  - a queued job leaves the queue (runner claimed it, freeing capacity)
  - a ranked success match is recorded (underplay measurement changed)

No polling, no timer. See migrations/001.sql and docs/09_ranking_system.md.
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from threading import Event

import psycopg

from sa_common.db.connection import get_conn
from sa_common.db.modes import Mode, list_modes
from sa_common.db.match_jobs import enqueue_match_job, count_queued_by_mode
from sa_common.types import SimArgs
from orchestrator.matchmaker import (
    VersionRef,
    list_underplayed_versions,
    pick_match_group,
)

log = logging.getLogger(__name__)


@dataclass
class SchedulerDaemonConfig:
    per_mode_queue_cap: int = 5      # max queued jobs per mode at any time


def _enqueue_one(
    conn: psycopg.Connection,
    mode: Mode,
    project_ids: list[int],
) -> int:
    """Enqueue a single job for the given mode and snapshot of project_ids."""
    sim_args = SimArgs.model_validate(mode.sim_args)
    with conn.transaction():
        return enqueue_match_job(
            conn,
            mode_id=mode.id,
            project_ids=project_ids,
            sim_args=sim_args,
            requested_by=None,
        )


def _schedule_solo_mode(
    conn: psycopg.Connection,
    mode: Mode,
    capacity: int,
) -> int:
    """Enqueue up to `capacity` solo jobs for underplayed versions. Returns the count enqueued."""
    underplayed = list_underplayed_versions(conn, mode)
    if not underplayed:
        return 0

    enqueued = 0
    for stats in underplayed:
        if enqueued >= capacity:
            break
        job_id = _enqueue_one(conn, mode, [stats.ref.project_id])
        log.info(
            "scheduler: mode=%s enqueued solo job id=%d for project=%d v%d",
            mode.slug, job_id, stats.ref.project_id, stats.ref.version,
        )
        enqueued += 1
    return enqueued


def _schedule_multi_mode(
    conn: psycopg.Connection,
    mode: Mode,
    capacity: int,
    rng: random.Random,
) -> int:
    """Enqueue up to `capacity` multi-player jobs. Returns the count enqueued.

    Each iteration re-reads underplayed versions because the previous enqueue
    in the same tick changes the picture (the seed's queued+played count goes
    up).
    """
    enqueued = 0
    while enqueued < capacity:
        underplayed = list_underplayed_versions(conn, mode)
        if not underplayed:
            break

        group = pick_match_group(conn, mode, underplayed, rng=rng)
        if group is None:
            log.info(
                "scheduler: mode=%s underplayed=%d but no group could be assembled "
                "(need %d participants)",
                mode.slug, len(underplayed), mode.participant_count,
            )
            break

        job_id = _enqueue_one(conn, mode, [v.project_id for v in group])
        log.info(
            "scheduler: mode=%s enqueued multi job id=%d projects=%s",
            mode.slug, job_id, [(v.project_id, v.version) for v in group],
        )
        enqueued += 1
    return enqueued


def run_one_iteration(
    conn: psycopg.Connection,
    config: SchedulerDaemonConfig,
    rng: random.Random | None = None,
) -> int:
    """Walk enabled modes and enqueue work. Returns total jobs enqueued."""
    rng = rng or random.Random()
    modes = list_modes(conn, enabled_only=True)
    if not modes:
        log.info("scheduler: no enabled modes; nothing to do")
        return 0

    queued_per_mode = count_queued_by_mode(conn)
    total_enqueued = 0

    for mode in modes:
        already_queued = queued_per_mode.get(mode.id, 0)
        capacity = config.per_mode_queue_cap - already_queued
        if capacity <= 0:
            log.debug("scheduler: mode=%s at queue cap (%d)", mode.slug, already_queued)
            continue

        if mode.participant_count == 1:
            total_enqueued += _schedule_solo_mode(conn, mode, capacity)
        else:
            total_enqueued += _schedule_multi_mode(conn, mode, capacity, rng)

    return total_enqueued


def run_forever(
    config: SchedulerDaemonConfig,
    shutdown: Event,
    wakeup: Event,
) -> None:
    """Event-driven main loop.

    Each cycle: enqueue what's enqueueable, then block on wakeup. NOTIFYs
    from the triggers in migrations/001.sql wake the loop the instant
    something might warrant new work.
    """
    from sa_common.db.notify import CHANNEL_SCHEDULER, start_listener

    log.info(
        "scheduler daemon starting (event-driven), per_mode_queue_cap=%d",
        config.per_mode_queue_cap,
    )
    start_listener([CHANNEL_SCHEDULER], wakeup, shutdown)
    rng = random.Random()

    with get_conn(autocommit=True) as conn:
        while not shutdown.is_set():
            wakeup.clear()
            try:
                count = run_one_iteration(conn, config, rng=rng)
                if count:
                    log.info("scheduler: enqueued %d job(s)", count)
            except psycopg.OperationalError:
                log.exception("DB connection failed; exiting")
                raise
            except Exception:
                log.exception("iteration failed unexpectedly")
            if shutdown.is_set():
                break
            wakeup.wait()

    log.info("scheduler daemon shut down cleanly")
