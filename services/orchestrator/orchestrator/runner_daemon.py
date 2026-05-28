# services/orchestrator/orchestrator/runner_daemon.py
"""The orchestrator's main loop.

Polls match_jobs for queued work, runs each job to completion via the runner,
and persists results. Owns all DB transactions; the runner is pure execution.

Transaction model:
    - Connection has autocommit=True at the connection level.
    - `with conn.transaction():` wraps the moments that must be atomic.
    - The match itself runs OUTSIDE any transaction so we never hold a row
      lock for the duration of a multi-minute sim.

Two atomic blocks per iteration:
    1. claim_one_queued_job — flips 'queued' → 'running' under SKIP LOCKED.
    2. record_match_result + mark_job_success — match row, participants,
       and job status all land together so a partial write can't lie about
       what completed.
"""
from __future__ import annotations

import logging
import shutil
import tempfile
import time
import uuid
import docker

from threading import Event
from datetime import datetime, timezone
from pathlib import Path

import psycopg

from runner.match import run_match
from runner.router import router_from_env, Router
from runner.match_results import build_participants
from sa_common.bundler import IBundler
from sa_common.db.match_jobs import (
    claim_one_queued_job,
    mark_job_failure,
    mark_job_success,
)
from sa_common.db.matches import record_match_result
from sa_common.db.connection import get_conn
from sa_common.types import SimArgs

from snake_sim.loop_observers.file_persist_observer import FilePersistObserver
from snake_sim.analyze.scripts.run_analyzer import analyze

from orchestrator.agents import SetupError, resolve_agents
from orchestrator.bundle import assemble_bundle

log = logging.getLogger(__name__)
d_client = docker.from_env()
router = router_from_env(d_client)

class RunnerDaemonConfig:
    """Static config for one daemon process."""
    def __init__(
        self,
        sim_image: str,
        bundler: IBundler,
        poll_interval_s: float = 1.0,
    ):
        self.sim_image = sim_image
        self.bundler = bundler
        self.poll_interval_s = poll_interval_s


def run_one_iteration(conn: psycopg.Connection, config: RunnerDaemonConfig) -> bool:
    """Run one claim-and-execute cycle. Returns True if work was done.

    Callers should sleep when this returns False, then loop again.
    """
    # --- Atomic block 1: claim ---
    with conn.transaction():
        job = claim_one_queued_job(conn)
    if job is None:
        return False

    log.info("processing job id=%d", job.id)
    match_uuid = f"match-{uuid.uuid4().hex[:8]}"
    started_at = datetime.now(timezone.utc)

    bundle_key = f"matches/{match_uuid}/bundle.zip"
    # Ephemeral working dir for the streamed replay; analyze() reads it, then it
    # goes into the bundle. Only the bundle is durably stored (via the bundler).
    work_dir = Path(tempfile.mkdtemp(prefix=f"match-{job.id}-"))
    replay_path = work_dir / "replay.json"
    file_observer = FilePersistObserver(store_dir=work_dir, filename="replay.json")

    try:
        sim_args = SimArgs.model_validate(job.sim_args)
        setup = resolve_agents(conn, job.project_ids)
        # --- The match itself: no transaction, no row locks held ---
        result = run_match(
            sim_image=config.sim_image,
            agents=setup.specs,
            sim_args=sim_args,
            match_id=match_uuid,
            router=router,
            d_client=d_client,
            extra_observers=[file_observer],
        )

        file_observer.close()  # flush the replay before reading it back

        # Analyze the captured replay so participants get per-snake outcomes and
        # the bundle carries analysis.json. Skip a zero-step match (analyze()
        # can't build a result from no steps).
        run_analysis = None
        if result.success and replay_path.exists() and replay_path.stat().st_size > 0:
            try:
                run_analysis = analyze(replay_path)
                result.run_analysis = run_analysis
            except Exception:
                log.warning("analysis failed for job id=%d", job.id, exc_info=True)

        participants = build_participants(
            result=result,
            project_by_agent_name=setup.project_by_name,
            version_by_agent_name=setup.version_by_name,
            seat_by_agent_name=setup.seat_by_name,
        )

        # Ranked bundle has no dev-agent console, so no agent_logs.
        saved_key: str | None = None
        try:
            bundle_bytes = assemble_bundle(replay_path, run_analysis, budgets=result.budgets)
            config.bundler.put(bundle_key, bundle_bytes)
            saved_key = bundle_key
            log.info("stored bundle %s (%d bytes)", bundle_key, len(bundle_bytes))
        except Exception:
            log.warning("failed to store bundle for job id=%d", job.id, exc_info=True)

        # --- Atomic block 2: persist match + flip job status together ---
        with conn.transaction():
            match_id = record_match_result(
                conn,
                match_uuid=match_uuid,
                status="success" if result.success else "failure",
                sim_args=sim_args,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
                bundle_key=saved_key,
                error=result.error,
                participants=participants,
            )
            mark_job_success(conn, job.id, match_id)

        log.info("job id=%d done (match_id=%d)", job.id, match_id)

    except SetupError as e:
        # Job is malformed (missing/unready submission, etc.). No match row to link.
        log.warning("job id=%d setup failed: %s", job.id, e)
        with conn.transaction():
            mark_job_failure(conn, job.id, f"setup: {e}")

    except Exception as e:
        # The match itself blew up in a way run_match didn't catch internally.
        log.exception("job id=%d crashed during execution", job.id)
        with conn.transaction():
            mark_job_failure(conn, job.id, repr(e))

    finally:
        try:
            file_observer.close()  # idempotent; ensures the writer thread is joined
        except Exception:
            pass
        shutil.rmtree(work_dir, ignore_errors=True)

    return True


def run_forever(config: RunnerDaemonConfig, shutdown: Event) -> None:
    """Main daemon loop. `shutdown` is a threading.Event; set it to stop."""
    log.info("orchestrator starting, polling every %.2fs", config.poll_interval_s)
    with get_conn(autocommit=True) as conn:
        while not shutdown.is_set():
            try:
                had_work = run_one_iteration(conn, config)
            except psycopg.OperationalError:
                # Connection-level problem; bubble up so the supervisor can restart us.
                log.exception("DB connection failed; exiting")
                raise
            except Exception:
                # Anything else: log and keep going. Don't let one bad iteration
                # take down the daemon.
                log.exception("iteration failed unexpectedly")
                had_work = False

            if not had_work:
                # Use Event.wait so SIGTERM interrupts the sleep promptly.
                shutdown.wait(timeout=config.poll_interval_s)

    log.info("orchestrator shut down cleanly")


# NOTE on stale 'running' jobs: if this process dies mid-match (OOM, host
# reboot), the job row stays in 'running' forever. Recovery is manual for
# now. When you need automatic recovery, add a heartbeat column and a
# reaper that resets rows whose heartbeat is older than some threshold.