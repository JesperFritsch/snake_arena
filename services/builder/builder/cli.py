
import argparse
import logging
import sys

from builder.build import build_project


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "project_id",
        type=int,
        help="Project ID to build",
    )
    
    parser.add_argument(
        "--log-level",
        default="INFO",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    result = build_project(
        project_id=args.project_id,
    )

    print(f"success: {result.success}")
    print(f"image: {result.image_tag}")
    print(f"duration: {result.duration_s:.2f}s")

    if result.error:
        print(f"error: {result.error}", file=sys.stderr)

    if result.build_logs:
        print("--- build logs ---")

        if isinstance(result.build_logs, str):
            print(result.build_logs)
        else:
            for line in result.build_logs:
                print(line)

    sys.exit(0 if result.success else 1)


if __name__ == "__main__":
    main()
