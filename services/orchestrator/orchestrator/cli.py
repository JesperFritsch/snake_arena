# services/orchestrator/orchestrator/cli.py
"""Entry point for the orchestrator daemon."""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from pathlib import Path

from orchestrator.daemon import OrchestratorConfig, run_forever


def main() -> None:
    parser = argparse.ArgumentParser(description="snake_arena orchestrator daemon")
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
        help="Seconds to sleep when the queue is empty",
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