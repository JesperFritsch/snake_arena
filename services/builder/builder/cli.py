import argparse
import logging
import sys
from pathlib import Path

from builder.build import build_submission, discover_languages


def main():
    available = sorted(discover_languages().keys())

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--language", required=True,
        choices=available or None,
        help=f"available: {available}",
    )
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--submission-id", required=True)
    parser.add_argument("--code-file", required=True, type=Path)
    parser.add_argument("--base-version", default="v1")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(message)s")

    result = build_submission(
        language=args.language,
        user_code=args.code_file,
        user_id=args.user_id,
        submission_id=args.submission_id,
        base_image_version=args.base_version,
    )

    print(f"success: {result.success}")
    print(f"image: {result.image_tag}")
    print(f"duration: {result.duration_s:.2f}s")
    if result.error:
        print(f"error: {result.error}", file=sys.stderr)
    if not result.success and result.build_logs:
        print("--- build logs ---", file=sys.stderr)
        print(result.build_logs, file=sys.stderr)

    sys.exit(0 if result.success else 1)


if __name__ == "__main__":
    main()