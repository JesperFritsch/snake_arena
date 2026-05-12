# services/runner/runner/match.py
import logging
import time
import uuid
import os
from dataclasses import dataclass
from pathlib import Path

import docker
from docker.errors import APIError, ImageNotFound, NotFound
from docker.models.containers import Container
from docker.models.networks import Network

log = logging.getLogger(__name__)


@dataclass
class AgentSpec:
    image: str
    name: str           # used as DNS name on the internal network


@dataclass
class MatchResult:
    success: bool
    sim_exit_code: int
    sim_logs: str
    agent_logs: dict[str, str]
    artifacts_path: Path | None
    error: str | None = None


def run_match(
    sim_image: str,
    agents: list[AgentSpec],
    sim_args: list[str],
    artifacts_host_dir: Path,
    *,
    match_id: str | None = None,
    agent_mem_limit: str = "512m",
    agent_cpus: float = 1.0,
    agent_pids_limit: int = 128,
    wall_timeout_s: int = 300,
    grpc_ready_timeout_s: int = 15,
) -> MatchResult:
    client = docker.from_env()
    match_id = match_id or f"match-{uuid.uuid4().hex[:8]}"
    network_name = match_id
    artifacts_host_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(artifacts_host_dir, 0o777)

    replay_filename = f"{match_id}.run_proto"

    network: Network | None = None
    agent_containers: list[Container] = []
    sim_container: Container | None = None

    try:
        log.info("creating network %s", network_name)
        network = client.networks.create(network_name, driver="bridge", internal=True)

        for agent in agents:
            log.info("starting agent %s (%s)", agent.name, agent.image)
            try:
                container = client.containers.run(
                    agent.image,
                    name=f"{match_id}-{agent.name}",
                    network=network_name,
                    hostname=agent.name,
                    detach=True,
                    remove=False,
                    read_only=True,
                    tmpfs={"/tmp": "size=64m"},
                    mem_limit=agent_mem_limit,
                    runtime="runsc",
                    memswap_limit=agent_mem_limit,
                    nano_cpus=int(agent_cpus * 1_000_000_000),
                    pids_limit=agent_pids_limit,
                    cap_drop=["ALL"],
                    security_opt=["no-new-privileges"],
                    user="1000:1000",
                )
                agent_containers.append(container)
            except ImageNotFound:
                return MatchResult(
                    success=False,
                    sim_exit_code=-1,
                    sim_logs="",
                    agent_logs={},
                    artifacts_path=None,
                    error=f"agent image not found: {agent.image}",
                )

        log.info("waiting for agents to be ready")
        if not _wait_for_agents_ready(agent_containers, timeout=grpc_ready_timeout_s):
            return MatchResult(
                success=False,
                sim_exit_code=-1,
                sim_logs="",
                agent_logs=_collect_agent_logs(agent_containers),
                artifacts_path=None,
                error="agents not ready within timeout",
            )

        targets = [f"{agent.name}:50051" for agent in agents]
        full_sim_args = [
            "compute",
            "--external-snake-targets", *targets,
            "--record-dir", "/tmp/runs",
            "--record-file", replay_filename,
            "--log-dir", "/tmp",
            "--no-render",
            *sim_args,
        ]

        log.info("starting sim with args: %s", full_sim_args)
        sim_container = client.containers.run(
            sim_image,
            command=full_sim_args,
            name=f"{match_id}-sim",
            network=network_name,
            volumes={str(artifacts_host_dir.resolve()): {"bind": "/tmp", "mode": "rw"}},
            detach=True,
            remove=False,
            mem_limit="2g",
        )

        try:
            result = sim_container.wait(timeout=wall_timeout_s)
            exit_code = result.get("StatusCode", -1)
        except Exception as e:
            log.warning("sim wait failed or timed out: %s", e)
            sim_container.kill()
            exit_code = -1

        sim_logs = sim_container.logs().decode(errors="replace")
        agent_logs = _collect_agent_logs(agent_containers)
        artifacts_path = artifacts_host_dir / "runs" /replay_filename
        artifacts_path = artifacts_path if artifacts_path.exists() else None
        print(f"artifacts_path: {artifacts_path}")
        return MatchResult(
            success=(exit_code == 0 and artifacts_path is not None),
            sim_exit_code=exit_code,
            sim_logs=sim_logs,
            agent_logs=agent_logs,
            artifacts_path=artifacts_path,
        )

    finally:
        _cleanup(sim_container, agent_containers, network)


def _wait_for_agents_ready(containers: list[Container], timeout: int) -> bool:
    deadline = time.monotonic() + timeout
    for c in containers:
        while time.monotonic() < deadline:
            c.reload()
            if c.status == "running":
                break
            if c.status in ("exited", "dead"):
                log.error("agent %s died before ready: status=%s", c.name, c.status)
                return False
            time.sleep(0.1)
        else:
            log.error("agent %s did not become running within timeout", c.name)
            return False
    # crude readiness: 0.5s grace for gRPC server bind. replace with a real probe later.
    time.sleep(0.5)
    return True


def _collect_agent_logs(containers: list[Container]) -> dict[str, str]:
    logs = {}
    for c in containers:
        try:
            logs[c.name] = c.logs().decode(errors="replace")
        except APIError:
            logs[c.name] = "<failed to collect logs>"
    return logs


def _cleanup(
    sim: Container | None,
    agents: list[Container],
    network: Network | None,
) -> None:
    for c in [sim, *agents]:
        if c is None:
            continue
        try:
            c.reload()
            if c.status == "running":
                c.stop(timeout=2)
            c.remove(force=True)
        except (NotFound, APIError) as e:
            log.warning("cleanup: %s", e)
    if network is not None:
        try:
            network.remove()
        except (NotFound, APIError) as e:
            log.warning("network cleanup: %s", e)
