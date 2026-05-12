# services/runner/runner/cli.py
import argparse
import logging
import sys
from pathlib import Path

from runner.match import AgentSpec, run_match


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sim-image", required=True)
    parser.add_argument(
        "--agent", action="append", required=True,
        help="image:name, repeatable. e.g. snake-submission-jesper-001:agent1",
    )
    parser.add_argument("--artifacts-dir", type=Path, default=Path("./sim-artifacts"))
    parser.add_argument("--sim-args", nargs=argparse.REMAINDER, default=[])
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(message)s")

    agents = []
    for spec in args.agent:
        image, _, name = spec.partition(":")
        if not name:
            print(f"invalid --agent spec: {spec} (expected image:name)", file=sys.stderr)
            sys.exit(2)
        agents.append(AgentSpec(image=image, name=name))

    result = run_match(
        sim_image=args.sim_image,
        agents=agents,
        sim_args=args.sim_args,
        artifacts_host_dir=args.artifacts_dir,
    )

    if result.artifacts_path:
        artifact_dir = result.artifacts_path.parent
    else:
        artifact_dir = args.artifacts_dir
    artifact_dir.mkdir(parents=True, exist_ok=True)

    (artifact_dir / "sim.log").write_text(result.sim_logs)
    for name, logs in result.agent_logs.items():
        (artifact_dir / f"{name}.log").write_text(logs)

    print(f"success: {result.success}")
    print(f"sim exit: {result.sim_exit_code}")
    print(f"artifacts: {result.artifacts_path}")
    if result.error:
        print(f"error: {result.error}")
    sys.exit(0 if result.success else 1)


if __name__ == "__main__":
    main()
