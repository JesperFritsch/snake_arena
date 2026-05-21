# services/orchestrator/orchestrator/cli.py
"""Entry point for the orchestrator.

Three daemons share this CLI:
    match        poll match_jobs, dispatch ranked matches via the runner
    test-match   poll test_match_jobs, build + run dev test matches
    (build daemon removed — dev builds happen inline in the test runner)

Each supports:
    (default)   long-running daemon, polls the queue forever
    --once      claim and process one job, then exit

Exit codes for --once:
    0   one job processed (whether it succeeded or failed)
    1   the daemon crashed while trying to process
    2   queue was empty; nothing to do
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from pathlib import Path
from typing import Any, Callable

import psycopg
from sa_common.db.connection import get_conn

from orchestrator.runner_daemon import (
    RunnerDaemonConfig,
    run_forever as run_match_forever,
    run_one_iteration as run_match_iteration,
)
from orchestrator.test_runner_daemon import (
    TestRunnerDaemonConfig,
    run_forever as run_test_forever,
    run_one_iteration as run_test_iteration,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="snake_arena orchestrator")
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))

    subparsers = parser.add_subparsers(dest="command", required=True)

    p_match = subparsers.add_parser("match", help="poll match_jobs and run matches")
    p_match.add_argument(
        "--sim-image",
        default=os.environ.get("ORCHESTRATOR_SIM_IMAGE"),
        help="Docker image tag for the sim (env: ORCHESTRATOR_SIM_IMAGE)",
    )
    p_match.add_argument(
        "--artifacts-dir",
        type=Path,
        default=Path(os.environ.get("ORCHESTRATOR_ARTIFACTS_DIR", "./sim-artifacts")),
        help="Host directory for match artifacts (env: ORCHESTRATOR_ARTIFACTS_DIR)",
    )
    _add_shared_args(p_match, default_poll=1.0, poll_env="ORCHESTRATOR_POLL_INTERVAL_S")

    p_test = subparsers.add_parser("test-match", help="poll test_match_jobs and run dev test matches")
    p_test.add_argument(
        "--sim-image",
        default=os.environ.get("ORCHESTRATOR_SIM_IMAGE"),
    )
    p_test.add_argument(
        "--artifacts-dir",
        type=Path,
        default=Path(os.environ.get("ORCHESTRATOR_ARTIFACTS_DIR", "./sim-artifacts")),
    )
    p_test.add_argument(
        "--redis-url",
        default=os.environ.get("REDIS_URL", "redis://localhost:6379"),
    )
    p_test.add_argument(
        "--registry-prefix",
        default=os.environ.get("BUILDER_REGISTRY_PREFIX", "snake"),
    )
    p_test.add_argument(
        "--build-timeout",
        type=int,
        default=int(os.environ.get("BUILDER_BUILD_TIMEOUT_S", "60")),
    )
    _add_shared_args(p_test, default_poll=1.0, poll_env="ORCHESTRATOR_POLL_INTERVAL_S")

    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger(f"orchestrator.{args.command}")

    if args.command == "match":
        if not args.sim_image:
            print("--sim-image or ORCHESTRATOR_SIM_IMAGE is required", file=sys.stderr)
            sys.exit(2)
        config = RunnerDaemonConfig(
            sim_image=args.sim_image,
            artifacts_dir=args.artifacts_dir,
            poll_interval_s=args.poll_interval,
        )
        run_one, run_forever = run_match_iteration, run_match_forever

    elif args.command == "test-match":
        if not args.sim_image:
            print("--sim-image or ORCHESTRATOR_SIM_IMAGE is required", file=sys.stderr)
            sys.exit(2)
        config = TestRunnerDaemonConfig(
            sim_image=args.sim_image,
            artifacts_dir=args.artifacts_dir,
            redis_url=args.redis_url,
            poll_interval_s=args.poll_interval,
            registry_prefix=args.registry_prefix,
            build_timeout_s=args.build_timeout,
        )
        run_one, run_forever = run_test_iteration, run_test_forever

    else:
        # argparse `required=True` should prevent this
        raise RuntimeError(f"unknown command: {args.command}")

    if args.once:
        _run_once(config, run_one, log)
    else:
        _run_daemon(config, run_forever, log)


def _add_shared_args(
    p: argparse.ArgumentParser, *, default_poll: float, poll_env: str
) -> None:
    p.add_argument(
        "--poll-interval",
        type=float,
        default=float(os.environ.get(poll_env, str(default_poll))),
        help="Seconds to sleep when the queue is empty (daemon mode only)",
    )
    p.add_argument(
        "--once",
        action="store_true",
        help="Process at most one job, then exit. Exit 2 if queue is empty.",
    )


def _run_once(
    config: Any,
    run_one: Callable[[psycopg.Connection, Any], bool],
    log: logging.Logger,
) -> None:
    """Single-shot mode: claim one job, run it, exit.

    Doesn't install signal handlers — Ctrl-C produces a normal
    KeyboardInterrupt, which is fine for one-shot use.
    """
    try:
        with get_conn(autocommit=True) as conn:
            had_work = run_one(conn, config)
    except KeyboardInterrupt:
        log.warning("interrupted")
        sys.exit(1)
    except Exception:
        log.exception("iteration failed")
        sys.exit(1)

    if had_work:
        log.info("processed one job; exiting")
        sys.exit(0)
    else:
        log.info("queue empty; nothing to do")
        sys.exit(2)


def _run_daemon(
    config: Any,
    run_forever: Callable[[Any, threading.Event], None],
    log: logging.Logger,
) -> None:
    """Long-running mode: poll the queue, process jobs as they appear.

    Installs SIGINT/SIGTERM handlers so the daemon finishes its current
    iteration cleanly instead of being killed mid-job.
    """
    shutdown = threading.Event()

    def _handle_signal(signum, _frame):
        log.info("received signal %d, finishing current iteration then exiting", signum)
        shutdown.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        run_forever(config, shutdown)
    except Exception:
        log.exception("daemon died")
        sys.exit(1)


if __name__ == "__main__":
    main()