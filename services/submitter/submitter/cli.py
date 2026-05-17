# services/submitter/submitter/cli.py
"""Manual submit CLI.

Until there's a web UI driving submits, this is how you promote a project
from dev to submitted.

Usage:
    submitter <project_id>
"""
from __future__ import annotations

import argparse
import logging
import sys

from submitter.submit import SubmitError, submit_project


def main() -> None:
    parser = argparse.ArgumentParser(description="Promote a project's dev build to submitted")
    parser.add_argument("project_id", type=int)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        result = submit_project(args.project_id)
    except SubmitError as e:
        print(f"submit refused: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"submitted as version {result.new_version}")
    print(f"image tag: {result.submitted_image_tag}")


if __name__ == "__main__":
    main()