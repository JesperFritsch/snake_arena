"""The build daemon's main loop.

Mirrors orchestrator.daemon: claim a build_jobs row atomically, run the build
OUTSIDE any transaction (no row locks held during a multi-second docker build),
then atomically record the outcome.

The builder owns projects.dev_* state via record_dev_build_*. This daemon only
manages the build_jobs row, with defensive fallbacks if the builder crashes or
returns failure without having marked the project failed itself.
"""
from __future__ import annotations

import logging
from threading import Event

import psycopg

from builder.build import build_project
from sa_common.db.build_jobs import (
    claim_one_queued_build_job,
    mark_build_job_failure,
    mark_build_job_success,
)
from sa_common.db.connection import get_conn
from sa_common.db.projects import record_dev_build_failure

log = logging.getLogger(__name__)


class BuildDaemonConfig:
    def __init__(
        self,
        registry_prefix: str = "snake",
        build_timeout_s: int = 60,
        poll_interval_s: float = 1.0,
    ):
        self.registry_prefix = registry_prefix
        self.build_timeout_s = build_timeout_s
        self.poll_interval_s = poll_interval_s


def run_one_iteration(conn: psycopg.Connection, config: BuildDaemonConfig) -> bool:
    """Run one claim-and-build cycle. Returns True if work was done."""
    # --- Atomic block 1: claim ---
    with conn.transaction():
        job = claim_one_queued_build_job(conn)
    if job is None:
        return False

    log.info("processing build job id=%d project_id=%d", job.id, job.project_id)

    try:
        # --- The build itself: no transaction, no row locks held ---
        result = build_project(
            project_id=job.project_id,
            registry_prefix=config.registry_prefix,
            build_timeout_s=config.build_timeout_s,
        )

        # --- Atomic block 2: record outcome ---
        with conn.transaction():
            if result.success:
                mark_build_job_success(conn, job.id)
            else:
                mark_build_job_failure(conn, job.id, result.error or "unknown error")
                # Defensive: builder's BuildError branch currently returns
                # without calling record_dev_build_failure, leaving the project
                # stuck at dev_build_status='building'. Force-correct it here.
                record_dev_build_failure(conn, job.project_id)

        log.info("build job id=%d done (success=%s)", job.id, result.success)

    except Exception as e:
        log.exception("build job id=%d crashed during execution", job.id)
        with conn.transaction():
            mark_build_job_failure(conn, job.id, repr(e))
            record_dev_build_failure(conn, job.project_id)

    return True


def run_forever(config: BuildDaemonConfig, shutdown: Event) -> None:
    """Main daemon loop. `shutdown` is a threading.Event; set it to stop."""
    log.info("build daemon starting, polling every %.2fs", config.poll_interval_s)
    with get_conn(autocommit=True) as conn:
        while not shutdown.is_set():
            try:
                had_work = run_one_iteration(conn, config)
            except psycopg.OperationalError:
                log.exception("DB connection failed; exiting")
                raise
            except Exception:
                log.exception("iteration failed unexpectedly")
                had_work = False
            if not had_work:
                shutdown.wait(timeout=config.poll_interval_s)
    log.info("build daemon shut down cleanly")