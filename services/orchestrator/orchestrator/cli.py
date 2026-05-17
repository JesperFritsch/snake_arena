# services/orchestrator/orchestrator/cli.py
"""Entry point for the orchestrator.

Two modes:
    (default)   long-running daemon, polls the queue forever
    --once      claim and process one job, then exit

Exit codes for --once:
    0   one job processed (whether the job itself succeeded or failed)
    1   the orchestrator crashed while trying to process
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

from sa_common.db.connection import get_conn

from orchestrator.daemon import OrchestratorConfig, run_forever, run_one_iteration


def main() -> None:
    parser = argparse.ArgumentParser(description="snake_arena orchestrator")
    parser.add_argument(
        "--sim-image",
        default=os.environ.get("ORCHESTRATOR_SIM_IMAGE"),
        help="Docker image tag for the sim (env: ORCHESTRATOR_SIM_IMAGE)",
    )
    parser.add_argument(
        "--artifacts-dir",
        type=Path,
        default=Path(os.environ.get("ORCHESTRATOR_ARTIFACTS_DIR", "./sim-artifacts")),
        help="Host directory for match artifacts (env: ORCHESTRATOR_ARTIFACTS_DIR)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=float(os.environ.get("ORCHESTRATOR_POLL_INTERVAL_S", "1.0")),
        help="Seconds to sleep when the queue is empty (daemon mode only)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process at most one job, then exit. Exit 2 if queue is empty.",
    )
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("orchestrator")

    if not args.sim_image:
        print("--sim-image or ORCHESTRATOR_SIM_IMAGE is required", file=sys.stderr)
        sys.exit(2)

    config = OrchestratorConfig(
        sim_image=args.sim_image,
        artifacts_dir=args.artifacts_dir,
        poll_interval_s=args.poll_interval,
    )

    if args.once:
        _run_once(config, log)
        return  # unreachable; _run_once always exits

    _run_daemon(config, log)


def _run_once(config: OrchestratorConfig, log: logging.Logger) -> None:
    """Single-shot mode: claim one job, run it, exit.

    Doesn't install signal handlers — Ctrl-C produces a normal
    KeyboardInterrupt, which is fine for one-shot use.
    """
    try:
        with get_conn(autocommit=True) as conn:
            had_work = run_one_iteration(conn, config)
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


def _run_daemon(config: OrchestratorConfig, log: logging.Logger) -> None:
    """Long-running mode: poll the queue, process jobs as they appear.

    Installs SIGINT/SIGTERM handlers so the daemon finishes its current
    iteration cleanly instead of being killed mid-match.
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