# services/runner/runner/match.py
"""Pure match execution. No database access.

Given a sim image, agent specs, and sim args, this module starts the
containers, runs the match, collects logs and analysis, and returns a
MatchResult. Persistence is the orchestrator's job.
"""
import logging
import os
import socket
import time
import uuid

from dataclasses import dataclass
from pathlib import Path

import docker
from docker import DockerClient
from docker.errors import APIError, ImageNotFound, NotFound
from docker.models.containers import Container
from docker.models.networks import Network

from snake_sim.environment.interfaces.loop_observable_interface import ILoopObservable
from snake_sim.environment.interfaces.loop_observer_interface import ILoopObserver
from snake_sim.loop_observables.socket_observable import SocketObservable
from snake_sim.analyze.scripts.run_analyzer import analyze

from runner.agent_container_manager import AgentContainerManager
from sa_common.types import MatchResult, SimArgs
from runner.router import Router


log = logging.getLogger(__name__)


@dataclass
class AgentSpec:
    image: str
    name: str           # used as DNS name on the agent's private network
    

def run_match(
    sim_image: str,
    agents: list[AgentSpec],
    sim_args: SimArgs,
    artifacts_host_dir: Path,
    *,
    router: Router,
    d_client: DockerClient,
    match_id: str | None = None,
    agent_mem_limit: str = "512m",
    agent_cpus: float = 1.0,
    agent_pids_limit: int = 128,
    grpc_ready_timeout_s: int = 15,
    extra_observers: list[ILoopObserver] | None = None,
    artifacts_local_dir: Path | None = None,
) -> MatchResult:
    match_id = match_id or f"match-{uuid.uuid4().hex[:8]}"
    # artifacts_local_dir is the path reachable by this process (inside the
    # runner container). artifacts_host_dir is what the Docker daemon sees when
    # mounting into the sim container. They differ when the runner is itself
    # containerised with a bind-mounted artifacts volume.
    local_dir = artifacts_local_dir or artifacts_host_dir
    local_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(local_dir, 0o777)

    replay_filename = f"{match_id}.run_proto"

    networks: list[Network] = []
    agent_containers: list[Container] = []
    sim_container: Container | None = None


    try:
        # one private network per agent — agents can't see each other
        for agent in agents:
            net_name = f"{match_id}-{agent.name}"
            log.info("creating network %s", net_name)
            networks.append(
                d_client.networks.create(net_name, driver="bridge", internal=True)
            )

        target_to_container: dict[str, Container] = {}
        target_to_name: dict[str, str] = {}
        targets: list[str] = []
        for agent, net in zip(agents, networks):
            target = f"{agent.name}:50051"
            targets.append(target)
            log.info("starting agent %s (%s) on %s", agent.name, agent.image, net.name)
            try:
                container = d_client.containers.run(
                    agent.image,
                    name=f"{match_id}-{agent.name}",
                    network=net.name,
                    hostname=agent.name,
                    detach=True,
                    remove=False,
                    # runtime="runsc",
                    read_only=True,
                    tmpfs={"/tmp": "size=64m"},
                    mem_limit=agent_mem_limit,
                    memswap_limit=agent_mem_limit,
                    nano_cpus=int(agent_cpus * 1_000_000_000),
                    pids_limit=agent_pids_limit,
                    cap_drop=["ALL"],
                    security_opt=["no-new-privileges"],
                    user="1000:1000",
                )
                agent_containers.append(container)
                target_to_container[target] = container
                target_to_name[target] = agent.name
            except ImageNotFound:
                return MatchResult(
                    success=False,
                    error=f"agent image not found: {agent.image}",
                )

        log.info("waiting for agents to be ready")
        if not _wait_for_agents_ready(agent_containers, timeout=grpc_ready_timeout_s):
            return MatchResult(
                success=False,
                agent_logs=_collect_agent_logs(agent_containers),
                error="agents not ready within timeout",
            )

        # Notify all observers (cpu + extra) that containers are ready so they
        # can start monitoring from the very first gRPC init call.
        seat_to_container = {i: c for i, c in enumerate(agent_containers)}
        cpu_observer = AgentContainerManager(
            snake_name_to_container=target_to_container,
            per_step_budget_seconds=0.05,
            initial_budget_seconds=0.2,
            startup_budget_seconds=0.2,
            poll_interval_s=0.01,
        )
        cpu_observer.set_agent_containers(seat_to_container)
        for obs in (extra_observers or []):
            if hasattr(obs, "set_agent_containers"):
                obs.set_agent_containers(seat_to_container)

        sim_net = d_client.networks.create(f"{match_id}-sim", driver="bridge", internal=False)
        networks.append(sim_net)
        router.attach(sim_net)
        loop_observable = SocketObservable()
        observable_addr = router.address_for(sim_net, loop_observable.port)

        full_sim_args = [
            "compute",
            "--ext-targets", *targets,
            "--ext-conn-timeout", "0.1",   # time to ESTABLISH the gRPC channel (agent boot)
            "--ext-init-timeout", "0.05",  # per-call deadline once connected (50ms)
            # "--decision-timeout-ms", "0", # this is enforced by the AgentContainerManager
            "--record-dir", "/tmp/runs",
            "--record-file", replay_filename,
            "--log-dir", "/tmp",
            "--no-render",
            "--snake-count", "0",  # don't run any inproc snakes
            "--socket-observer", observable_addr,
            *sim_args.to_args(),
        ]

        loop_observable.add_observer(cpu_observer)
        for obs in (extra_observers or []):
            loop_observable.add_observer(obs)
        loop_observable.start()

        log.info("starting sim with args: %s", full_sim_args)
        sim_container = d_client.containers.run(
            sim_image,
            command=full_sim_args,
            network=sim_net.name,
            name=f"{match_id}-sim",
            volumes={str(artifacts_host_dir.resolve()): {"bind": "/tmp", "mode": "rw"}},
            detach=True,
            remove=False,
            mem_limit="2g",
            # Sim is trusted (only agents are sandboxed). Run as root so it can
            # always write replay/log files to the bind-mounted artifacts dir
            # regardless of host-side ownership.
            user="root",
        )

        # attach sim to each agent network so it can reach every agent
        for net in networks[:-1]:
            net.connect(sim_container)

        try:
            ex_result = sim_container.wait()
            exit_code = ex_result.get("StatusCode", -1)
        except Exception as e:
            log.warning("sim wait failed: %s", e)
            try:
                sim_container.kill()
            except (NotFound, APIError) as kill_err:
                log.warning("failed to kill sim container: %s", kill_err)
            exit_code = -1

        sim_logs = sim_container.logs().decode(errors="replace")
        agent_logs = _collect_agent_logs(agent_containers)
        replay_path = artifacts_host_dir / "runs" / replay_filename
        replay_path = replay_path if replay_path.exists() else None

        run_analysis = None
        if exit_code == 0 and replay_path is not None:
            try:
                run_analysis = analyze(replay_path)
            except Exception as e:
                log.warning("analysis failed: %s", e)

        return MatchResult(
            success=(exit_code == 0 and replay_path is not None),
            sim_logs=sim_logs,
            agent_logs=agent_logs,
            tags_to_names=target_to_name,
            replay_path=replay_path,
            run_analysis=run_analysis,
        )

    finally:
        _cleanup(
            sim_container, 
            agent_containers, 
            networks, 
            router, 
            loop_observable
        )


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


def _safe(fn, *args, _label: str = "", **kwargs) -> None:
    """Run fn(*args, **kwargs), logging any exception (including KeyboardInterrupt)."""
    try:
        fn(*args, **kwargs)
    except KeyboardInterrupt:
        log.warning("cleanup interrupted at: %s — continuing", _label)
    except Exception as e:
        log.warning("cleanup %s failed: %s", _label, e)


def _stop_and_remove_container(c: Container) -> None:
    try:
        c.reload()
    except (NotFound, APIError):
        # gone already
        return
    if c.status == "running":
        try:
            c.kill()   # don't wait for graceful stop in cleanup; agents are untrusted
        except (NotFound, APIError):
            pass
    try:
        c.remove(force=True)
    except (NotFound, APIError):
        pass


def _cleanup(
    sim: Container | None,
    agents: list[Container],
    networks: list[Network],
    router: Router,
    loop_observable: ILoopObservable | None,
) -> None:
    # CONTAINERS FIRST — these are the resources that matter most.
    # Each call is isolated so one failure (or Ctrl-C) doesn't abort the rest.
    for c in [sim, *agents]:
        if c is None:
            continue
        _safe(_stop_and_remove_container, c, _label=f"container {c.name}")

    # Networks next — quick, but only useful once their containers are gone.
    for net in networks:
        _safe(router.detach, net, _label=f"Router detach {net.name}")
        _safe(net.remove, _label=f"network {net.name}")

    # Observable last — may block on thread join, but no resources leak if it does.
    if loop_observable is not None:
        _safe(loop_observable.stop, _label="loop observable")
    