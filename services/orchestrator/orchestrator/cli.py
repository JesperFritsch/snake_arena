# services/orchestrator/orchestrator/cli.py
"""Entry point for the orchestrator.

Three daemons share this CLI:
    match        run queued ranked match jobs from the runner
    test-match   build + run queued dev test matches
    scheduler    enqueue ranked match jobs as work appears

All three are event-driven via Postgres LISTEN/NOTIFY — they have no polling
interval. Triggers in migrations/001.sql wake each daemon the moment its
queue gains work. Scoring is no longer a daemon: aggregates are computed on
demand in sa_common.db.agent_scores.compute_mode_scores. See docs/09_ranking_system.md.

Each supports:
    (default)   long-running daemon, sleeps between NOTIFYs
    --once      claim and process one job, then exit (match / test-match only)

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
import socket
import sys
import threading
from typing import Any, Callable

import psycopg
from sa_common.bundler import bundler_from_env
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
from orchestrator.scheduler_daemon import (
    SchedulerDaemonConfig,
    run_forever as run_scheduler_forever,
)
from sa_common.db.test_match_jobs import reset_stale_running_jobs


def _env_or(cli_value, env_name: str, cast=str):
    """Resolve a flag's value: CLI override if given, else os.environ[env_name].

    Keeps env reads out of argparse defaults so each subcommand only requires
    the env vars it actually uses — e.g. running `scheduler` doesn't fail
    because BUILDER_BUILD_TIMEOUT_S (only needed by `test-match`) isn't set.

    Missing env exits with a short message naming the variable. Cast failures
    bubble up as-is — they're a config-typo class of bug, not a missing one.
    """
    if cli_value is not None:
        return cli_value
    try:
        return cast(os.environ[env_name])
    except KeyError:
        sys.exit(f"missing required environment variable: {env_name}")


def main() -> None:
    parser = argparse.ArgumentParser(description="gridsnake orchestrator")
    parser.add_argument("--log-level", default=None)

    subparsers = parser.add_subparsers(dest="command", required=True)

    p_match = subparsers.add_parser("match", help="run queued ranked match jobs")
    p_match.add_argument(
        "--sim-image", default=None,
        help="Docker image tag for the sim (env: ORCHESTRATOR_SIM_IMAGE)",
    )
    p_match.add_argument(
        "--once", action="store_true",
        help="Process at most one job, then exit. Exit 2 if queue is empty.",
    )
    p_match.add_argument(
        "--step-cpu-budget-ms", type=int, default=None,
        help="Override per-step CPU budget for ranked matches (env: STEP_CPU_BUDGET_MS)",
    )

    p_test = subparsers.add_parser("test-match", help="run queued dev test matches")
    p_test.add_argument("--sim-image",      default=None)
    p_test.add_argument("--redis-url",      default=None)
    p_test.add_argument("--registry-prefix", default=None)
    p_test.add_argument("--build-timeout", type=int, default=None)
    p_test.add_argument(
        "--once", action="store_true",
        help="Process at most one job, then exit. Exit 2 if queue is empty.",
    )
    p_test.add_argument(
        "--step_cpu_budget_ms", type=int, default=None,
        help="Override per-step CPU budget for test matches (env: STEP_CPU_BUDGET_MS)",
    )

    p_sched = subparsers.add_parser(
        "scheduler",
        help="enqueue ranked match jobs. Match configs (sim_args, target counts, "
             "etc.) live on the modes table — this daemon has no per-mode flags.",
    )
    p_sched.add_argument(
        "--per-mode-queue-cap", type=int, default=None,
        help="Max queued jobs per mode at any time (env: SCHEDULER_PER_MODE_QUEUE_CAP)",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=_env_or(args.log_level, "LOG_LEVEL"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger(f"orchestrator.{args.command}")

    # Container hostname is compose-assigned and stable across `compose
    # restart` per replica, so it doubles as the runner's docker-label id.
    runner_id = socket.gethostname()

    if args.command == "match":
        config = RunnerDaemonConfig(
            sim_image=_env_or(args.sim_image, "ORCHESTRATOR_SIM_IMAGE"),
            bundler=bundler_from_env(),
            runner_id=runner_id,
        )
        run_one, run_forever = run_match_iteration, run_match_forever

    elif args.command == "test-match":
        config = TestRunnerDaemonConfig(
            sim_image=_env_or(args.sim_image, "ORCHESTRATOR_SIM_IMAGE"),
            bundler=bundler_from_env(),
            runner_id=runner_id,
            redis_url=_env_or(args.redis_url, "REDIS_URL"),
            registry_prefix=_env_or(args.registry_prefix, "BUILDER_REGISTRY_PREFIX"),
            build_timeout_s=_env_or(args.build_timeout, "BUILDER_BUILD_TIMEOUT_S", int),
            test_per_step_budget_seconds=_env_or(args.step_cpu_budget_ms, "STEP_CPU_BUDGET_MS", lambda x: float(x) / 1000),
        )
        run_one, run_forever = run_test_iteration, run_test_forever
        with get_conn(autocommit=True) as conn:
            reset_stale_running_jobs(conn)

    elif args.command == "scheduler":
        config = SchedulerDaemonConfig(
            per_mode_queue_cap=_env_or(args.per_mode_queue_cap, "SCHEDULER_PER_MODE_QUEUE_CAP", int),
        )
        # Scheduler doesn't support --once (it's purely additive, not claim-based).
        _run_daemon(config, run_scheduler_forever, log)
        return

    else:
        # argparse `required=True` should prevent this
        raise RuntimeError(f"unknown command: {args.command}")

    if args.once:
        _run_once(config, run_one, log)
    else:
        _run_daemon(config, run_forever, log)


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
    run_forever: Callable[[Any, threading.Event, threading.Event], None],
    log: logging.Logger,
) -> None:
    """Long-running mode: process jobs as NOTIFYs arrive.

    Installs SIGINT/SIGTERM handlers that set both `shutdown` and `wakeup`
    so a daemon blocked inside wakeup.wait() exits promptly instead of
    being killed.
    """
    shutdown = threading.Event()
    wakeup = threading.Event()

    def _handle_signal(signum, _frame):
        log.info("received signal %d, finishing current iteration then exiting", signum)
        shutdown.set()
        wakeup.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        run_forever(config, shutdown, wakeup)
    except Exception:
        log.exception("daemon died")
        sys.exit(1)


if __name__ == "__main__":
    main()