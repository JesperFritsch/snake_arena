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

import logging
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

from orchestrator.agents import SetupError, resolve_test_agents
from orchestrator.redis_observer import RedisStreamObserver

log = logging.getLogger(__name__)
d_client = docker.from_env()
router = router_from_env(d_client)


class TestRunnerDaemonConfig:
    def __init__(
        self,
        sim_image: str,
        artifacts_dir: Path,
        redis_url: str = "redis://localhost:6379",
        poll_interval_s: float = 1.0,
        registry_prefix: str = "snake",
        build_timeout_s: int = 60,
    ):
        self.sim_image = sim_image
        self.artifacts_dir = artifacts_dir
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

    bundle_relative = f"test-matches/{job.id}/bundle.zip"
    bundle_abs = config.artifacts_dir / bundle_relative

    redis_client = redis.Redis.from_url(config.redis_url, socket_connect_timeout=5)
    observer = RedisStreamObserver(
        redis_client=redis_client,
        channel=f"test-match:{job.id}",
        bundle_path=bundle_abs,
    )

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
                raise SetupError(f"build failed: {build_result.error}")

        setup = resolve_test_agents(conn, job.player_project_id, job.opponent_project_ids)

        result = run_match(
            sim_image=config.sim_image,
            agents=setup.specs,
            sim_args=sim_args,
            artifacts_host_dir=config.artifacts_dir,
            match_id=match_uuid,
            router=router,
            d_client=d_client,
            extra_observers=[observer],
        )

        participants = build_participants(
            result=result,
            project_by_agent_name=setup.project_by_name,
            version_by_agent_name=setup.version_by_name,
            seat_by_agent_name=setup.seat_by_name,
        )

        saved_bundle = bundle_abs.exists()

        with conn.transaction():
            match_id = record_match_result(
                conn,
                match_uuid=match_uuid,
                status="success" if result.success else "failure",
                sim_args=sim_args,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
                replay_r2_key=str(result.replay_path) if result.replay_path else None,
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
