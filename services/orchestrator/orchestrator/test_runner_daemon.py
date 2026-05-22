# services/orchestrator/orchestrator/test_runner_daemon.py
"""Daemon for user-initiated test matches.

Mirrors runner_daemon but reads from test_match_jobs and resolves agents
asymmetrically: player uses dev_image_tag, opponents use submitted_image_tag.
Resulting matches are recorded with is_test=True so the leaderboard can
exclude them.

During each match a RedisStreamObserver publishes JSON frames to a Redis
pub/sub channel (test-match:{job_id}) for live browser streaming, and saves
a .json.gz replay file to artifacts_dir at the end.
"""
from __future__ import annotations

import io
import json
import logging
import time
import uuid
import zipfile
import docker
import redis

from threading import Event
from datetime import datetime, timezone
from pathlib import Path

import psycopg

from builder.build import build_project
from runner.match import run_match
from runner.router import router_from_env, Router
from runner.match_results import build_participants
from sa_common.db.projects import get_project_meta
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
from orchestrator.redis_observer import RedisStreamObserver

log = logging.getLogger(__name__)
d_client = docker.from_env()
router = router_from_env(d_client)


def _build_bundle(
    bundle_path: Path,
    replay_path: Path,
    dev_step_logs: list[str] | None,
) -> bool:
    """Zip the test-match artifacts (replay + dev-agent logs) into bundle_path.

    Returns True if the bundle was written. analysis.json is a placeholder
    until the frontend consumes run_analyzer output.
    """
    try:
        bundle_path.parent.mkdir(parents=True, exist_ok=True)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            if replay_path.exists():
                zf.writestr("replay.json", replay_path.read_bytes())
            zf.writestr("agent_logs.json", json.dumps({"0": dev_step_logs or []}).encode())
            zf.writestr("analysis.json", b"{}")
        bundle_path.write_bytes(buf.getvalue())
        log.info("saved bundle to %s (%d bytes)", bundle_path, bundle_path.stat().st_size)
        return True
    except Exception:
        log.warning("failed to save bundle to %s", bundle_path, exc_info=True)
        return False


class TestRunnerDaemonConfig:
    def __init__(
        self,
        sim_image: str,
        artifacts_dir: Path,
        artifacts_host_dir: Path,
        redis_url: str = "redis://localhost:6379",
        poll_interval_s: float = 1.0,
        registry_prefix: str = "snake",
        build_timeout_s: int = 60,
    ):
        self.sim_image = sim_image
        self.artifacts_dir = artifacts_dir          # path inside this container
        self.artifacts_host_dir = artifacts_host_dir  # path on the Docker host
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

    job_dir = config.artifacts_dir / "test-matches" / str(job.id)
    replay_path = job_dir / "replay.json"
    bundle_relative = f"test-matches/{job.id}/bundle.zip"
    bundle_abs = config.artifacts_dir / bundle_relative

    redis_client = redis.Redis.from_url(config.redis_url, socket_connect_timeout=5)
    observer = RedisStreamObserver(
        redis_client=redis_client,
        channel=f"test-match:{job.id}",
    )
    # Captures the replay to job_dir/replay.json in-process (no volume mount,
    # no sim-side recording). Created here so the finally block can close it.
    file_observer = FilePersistObserver(store_dir=job_dir, filename="replay.json")

    try:
        sim_args = SimArgs.model_validate(job.sim_args)

        # Build the player's dev image if it isn't already ready.
        player = get_project_meta(conn, job.player_project_id)
        if player is None:
            raise SetupError(f"player project {job.player_project_id} not found")
        if player.dev_build_status != "ready":
            log.info(
                "test job id=%d: player project %d needs build (status=%s)",
                job.id, job.player_project_id, player.dev_build_status,
            )
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
                raise SetupError(f"build failed:\n{detail}")

        setup = resolve_test_agents(conn, job.player_project_id, job.opponent_project_ids)

        result = run_match(
            sim_image=config.sim_image,
            agents=setup.specs,
            sim_args=sim_args,
            artifacts_host_dir=config.artifacts_host_dir,
            artifacts_local_dir=config.artifacts_dir,
            match_id=match_uuid,
            router=router,
            d_client=d_client,
            extra_observers=[observer, file_observer],
        )

        # Flush the in-process replay writer before reading the file back.
        file_observer.close()

        # Emit terminal frames to any live viewer: logs first, then stop.
        # The API closes the stream on "stop", so logs must precede it. Done
        # before analyze/bundle so viewers aren't blocked on post-processing.
        observer.publish_logs(result.dev_agent_step_logs)
        observer.publish_stop()

        # Analyze the captured replay; populate result.run_analysis so
        # build_participants can derive per-snake outcomes. Skip an empty
        # replay (match streamed nothing) — analyze() would choke on it.
        if result.success and replay_path.exists() and replay_path.stat().st_size > 0:
            try:
                result.run_analysis = analyze(replay_path)
            except Exception:
                log.warning("analysis failed for job id=%d", job.id, exc_info=True)

        participants = build_participants(
            result=result,
            project_by_agent_name=setup.project_by_name,
            version_by_agent_name=setup.version_by_name,
            seat_by_agent_name=setup.seat_by_name,
        )

        saved_bundle = _build_bundle(bundle_abs, replay_path, result.dev_agent_step_logs)

        with conn.transaction():
            match_id = record_match_result(
                conn,
                match_uuid=match_uuid,
                status="success" if result.success else "failure",
                sim_args=sim_args,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
                replay_r2_key=None,
                error=result.error,
                participants=participants,
                is_test=True,
            )
            mark_test_job_success(
                conn,
                job.id,
                match_id,
                bundle_path=bundle_relative if saved_bundle else None,
            )

        log.info("test job id=%d done (match_id=%d)", job.id, match_id)

    except SetupError as e:
        log.warning("test job id=%d setup failed: %s", job.id, e)
        with conn.transaction():
            mark_test_job_failure(conn, job.id, f"setup: {e}")

    except Exception as e:
        log.exception("test job id=%d crashed during execution", job.id)
        with conn.transaction():
            mark_test_job_failure(conn, job.id, repr(e))

    finally:
        try:
            file_observer.close()  # idempotent; ensures the writer thread is joined
        except Exception:
            pass
        try:
            redis_client.close()
        except Exception:
            pass

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
