# services/orchestrator/orchestrator/test_runner_daemon.py
"""Daemon for user-initiated test matches.

Mirrors runner_daemon but reads from test_match_jobs and resolves agents
asymmetrically: player uses dev_image_tag, opponents use submitted_image_tag.
Resulting matches are recorded with is_test=True so the leaderboard can
exclude them.

During each match a RedisStreamObserver publishes JSON frames to a Redis
pub/sub channel (test-match:{job_id}) for live browser streaming. The replay
is captured in-process to a temp dir, then assembled with the dev-agent logs
and run analysis into a bundle zip stored via the configured IBundler.
"""
from __future__ import annotations

import logging
import shutil
import tempfile
import time
import uuid
import docker
import redis

from threading import Event
from datetime import datetime, timezone
from pathlib import Path

import psycopg

from builder.build import build_project
from runner.match import run_match
from runner.router import router_from_env
from runner.match_results import build_participants
from sa_common.bundler import IBundler
from sa_common.db.projects import (
    get_project_meta,
    record_dev_build_validated,
    record_dev_build_crashed,
)
from sa_common.db.test_match_jobs import (
    claim_one_queued_test_job,
    mark_test_job_failure,
    mark_test_job_success,
)
from sa_common.db.matches import record_match_result
from sa_common.db.connection import get_conn
from sa_common.types import SimArgs

from snake_sim.loop_observers.file_persist_observer import FilePersistObserver
from snake_sim.analyze.scripts.run_analyzer import analyze

from orchestrator.agents import SetupError, resolve_test_agents
from orchestrator.bundle import assemble_bundle
from orchestrator.redis_observer import RedisStreamObserver

log = logging.getLogger(__name__)
d_client = docker.from_env()
router = router_from_env(d_client)


class TestRunnerDaemonConfig:
    def __init__(
        self,
        sim_image: str,
        bundler: IBundler,
        redis_url: str = "redis://localhost:6379",
        poll_interval_s: float = 1.0,
        registry_prefix: str = "snake",
        build_timeout_s: int = 60,
    ):
        self.sim_image = sim_image
        self.bundler = bundler
        self.redis_url = redis_url
        self.poll_interval_s = poll_interval_s
        self.registry_prefix = registry_prefix
        self.build_timeout_s = build_timeout_s


def run_one_iteration(conn: psycopg.Connection, config: TestRunnerDaemonConfig) -> bool:
    with conn.transaction():
        job = claim_one_queued_test_job(conn)
    if job is None:
        return False

    log.info("processing test match job id=%d", job.id)
    match_uuid = f"test-{uuid.uuid4().hex[:8]}"
    started_at = datetime.now(timezone.utc)

    bundle_key = f"test-matches/{job.id}/bundle.zip"
    # Ephemeral working dir for the streamed replay; analyze() reads it, then it
    # goes into the bundle. Only the bundle is durably stored (via the bundler).
    work_dir = Path(tempfile.mkdtemp(prefix=f"test-match-{job.id}-"))
    replay_path = work_dir / "replay.json"

    redis_client = redis.Redis.from_url(config.redis_url, socket_connect_timeout=5)
    observer = RedisStreamObserver(
        redis_client=redis_client,
        channel=f"test-match:{job.id}",
    )
    # Captures the replay to work_dir/replay.json in-process (no sim-side
    # recording). Created here so the finally block can close it.
    file_observer = FilePersistObserver(store_dir=work_dir, filename="replay.json")

    terminal_status: str | None = None  # finally publishes this if not already done
    published_terminal = False

    def on_match_result(result) -> None:
        # Fires inside run_match before its (slow) Docker teardown, so live
        # viewers get the outcome immediately. Persist + publish the dev-build
        # verdict HERE (not after run_match returns) so it can't be skipped by a
        # later exception (cleanup, analysis, bundling) — and it goes out before
        # the terminal status, which the API uses to close the stream.
        nonlocal published_terminal
        verdict = "ready" if observer.dev_reached_start else "crashed"
        try:
            with conn.transaction():
                if observer.dev_reached_start:
                    record_dev_build_validated(conn, job.player_project_id)
                else:
                    record_dev_build_crashed(conn, job.player_project_id)
        except Exception:
            log.warning("failed to persist dev-build verdict for job id=%d", job.id, exc_info=True)
        observer.publish_build(verdict)
        if observer.step_count == 0 and result.dev_agent_step_logs:
            observer.publish_step_log(0, result.dev_agent_step_logs[0])
        observer.publish_stop()
        observer.publish_status("success" if result.success else "failure")
        published_terminal = True

    try:
        observer.publish_status("running")
        sim_args = SimArgs.model_validate(job.sim_args)

        # (Re)build the player's dev image unless a current-code image already
        # exists. 'built'/'ready'/'crashed' all mean the current code compiled
        # (a save resets status to 'saved'); anything else needs a build.
        player = get_project_meta(conn, job.player_project_id)
        if player is None:
            raise SetupError(f"player project {job.player_project_id} not found")
        if player.dev_build_status not in ("built", "ready", "crashed"):
            log.info(
                "test job id=%d: player project %d needs build (status=%s)",
                job.id, job.player_project_id, player.dev_build_status,
            )
            observer.publish_build("building")
            build_result = build_project(
                project_id=job.player_project_id,
                registry_prefix=config.registry_prefix,
                build_timeout_s=config.build_timeout_s,
            )
            if not build_result.success:
                detail = build_result.build_logs or build_result.error or "unknown error"
                # Truncate to keep the DB column sane.
                if len(detail) > 8000:
                    detail = detail[-8000:]
                observer.publish_build("failed", error=detail)
                raise SetupError(f"build failed:\n{detail}")
            observer.publish_build("built")

        setup = resolve_test_agents(conn, job.player_project_id, job.opponent_project_ids)
        # The dev agent is seat 0; tell the observer its DNS name so it can
        # detect whether the dev (not an opponent) reached the match start.
        observer.dev_name = setup.specs[0].name

        result = run_match(
            sim_image=config.sim_image,
            agents=setup.specs,
            sim_args=sim_args,
            match_id=match_uuid,
            router=router,
            d_client=d_client,
            extra_observers=[observer, file_observer],
            on_step_log=observer.publish_step_log,
            on_result=on_match_result,
        )

        # Flush the in-process replay writer before reading the file back.
        # (Live viewers already got the verdict + stop/status via on_match_result,
        # and the dev-build verdict was persisted there too.)
        file_observer.close()

        # Analyze the captured replay so build_participants can derive per-snake
        # outcomes and the bundle carries analysis.json. Skip when the match had
        # no steps (e.g. the agent never connected) — analyze() can't build a
        # result from zero steps.
        run_analysis = None
        if result.success and observer.step_count > 0 and replay_path.exists():
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

        saved_key: str | None = None
        try:
            bundle_bytes = assemble_bundle(
                replay_path, run_analysis, dev_step_logs=result.dev_agent_step_logs or []
            )
            config.bundler.put(bundle_key, bundle_bytes)
            saved_key = bundle_key
            log.info("stored bundle %s (%d bytes)", bundle_key, len(bundle_bytes))
        except Exception:
            log.warning("failed to store bundle for job id=%d", job.id, exc_info=True)

        with conn.transaction():
            match_id = record_match_result(
                conn,
                match_uuid=match_uuid,
                status="success" if result.success else "failure",
                sim_args=sim_args,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
                # Test-match bundle is stored on the job row (bundle_key above),
                # not the match row.
                bundle_key=None,
                error=result.error,
                participants=participants,
                is_test=True,
            )
            mark_test_job_success(
                conn,
                job.id,
                match_id,
                bundle_key=saved_key,
            )

        terminal_status = "success" if result.success else "failure"
        log.info("test job id=%d done (match_id=%d)", job.id, match_id)

    except SetupError as e:
        log.warning("test job id=%d setup failed: %s", job.id, e)
        with conn.transaction():
            mark_test_job_failure(conn, job.id, f"setup: {e}")
        terminal_status = "failure"

    except Exception as e:
        log.exception("test job id=%d crashed during execution", job.id)
        with conn.transaction():
            mark_test_job_failure(conn, job.id, repr(e))
        terminal_status = "failure"

    finally:
        # Terminal status closes the WS for live viewers. Normally on_match_result
        # already published it (before teardown); this is the fallback for paths
        # that never reached run_match (build failure, setup error, early crash).
        try:
            if not published_terminal:
                observer.publish_status(terminal_status or "failure")
        except Exception:
            log.warning("failed to publish terminal status for job id=%d", job.id, exc_info=True)
        try:
            file_observer.close()  # idempotent; ensures the writer thread is joined
        except Exception:
            pass
        try:
            redis_client.close()
        except Exception:
            pass
        shutil.rmtree(work_dir, ignore_errors=True)  # drop the ephemeral replay

    return True


def run_forever(config: TestRunnerDaemonConfig, shutdown: Event) -> None:
    log.info("test match daemon starting, polling every %.2fs", config.poll_interval_s)
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

    log.info("test match daemon shut down cleanly")
