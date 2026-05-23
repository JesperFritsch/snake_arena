# services/runner/runner/match.py
"""Pure match execution. No database access.

Given a sim image, agent specs, and sim args, this module starts the
containers, runs the match, collects logs and analysis, and returns a
MatchResult. Persistence is the orchestrator's job.
"""
import logging
import os
import socket
import threading
import time
import uuid

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import docker
from docker import DockerClient
from docker.errors import APIError, ImageNotFound, NotFound
from docker.models.containers import Container
from docker.models.networks import Network

from snake_sim.environment.interfaces.loop_observable_interface import ILoopObservable
from snake_sim.environment.interfaces.loop_observer_interface import ILoopObserver
from snake_sim.loop_observables.socket_observable import SocketObservable

from runner.agent_container_manager import AgentContainerManager
from sa_common.types import MatchResult, SimArgs
from runner.router import Router


log = logging.getLogger(__name__)

_STEP_SEP = "---STEP_END---\n"  # printed by the agent harness after each update()
_STEP_STDOUT_BUDGET = 2_000     # bytes per step chunk before truncation notice
_STARTUP_LOG_BUDGET = 16_000    # bytes kept when an agent crashes before any step
_TRUNCATION_NOTICE = "\n[stdout truncated — output too long for this step]\n"


def _split_step_logs(text: str, budget: int = _STEP_STDOUT_BUDGET) -> list[str]:
    """Split an agent's stdout into per-frame chunks on the harness separator.

    out[S] is the agent's stdout from the update() that reacted to frame S's
    world (chunk S), so it lines up with frame S on screen. Each chunk is
    capped so a chatty step can't crowd out later ones.
    """
    chunks = text.split(_STEP_SEP)
    if chunks and not chunks[-1].strip():
        chunks = chunks[:-1]
    out: list[str] = []
    for chunk in chunks:
        if len(chunk.encode()) > budget:
            chunk = chunk.encode()[:budget].decode(errors="replace") + _TRUNCATION_NOTICE
        out.append(chunk)
    return out


def _tail_log(text: str, budget: int = _STARTUP_LOG_BUDGET) -> str:
    """Last `budget` bytes of text — for a crash before any step, where the
    traceback is at the end of the output."""
    b = text.encode()
    if len(b) <= budget:
        return text
    return "[earlier output truncated]\n" + b[-budget:].decode(errors="replace")


def _dev_step_logs(dev_text: str) -> list[str] | None:
    """Dev-agent (seat 0) console logs. If the agent took steps, split into
    per-frame chunks. If it crashed before the first update (no separators),
    return the whole log as a single step-0 entry so the console still shows
    what happened."""
    if not dev_text.strip():
        return None
    if _STEP_SEP in dev_text:
        return _split_step_logs(dev_text)
    return [_tail_log(dev_text)]


class _StepLogStreamer:
    """Follows a container's stdout in a daemon thread and invokes
    on_step_log(frame, text) as each frame's output completes (delimited by the
    harness separator). Chunk c is the agent reacting to frame c's world, so it
    maps to frame c.

    Each frame is capped at `budget` bytes *mid-stream*: once it exceeds the
    budget, further bytes are dropped (memory stays bounded even if an agent
    floods stdout) and the cap resets at the next separator. Best-effort,
    live-only — the bundle uses _split_step_logs.
    """

    def __init__(
        self,
        container: Container,
        on_step_log: Callable[[int, str], None],
        budget: int = _STEP_STDOUT_BUDGET,
    ) -> None:
        self._container = container
        self._on_step_log = on_step_log
        self._budget = budget
        self._thread = threading.Thread(target=self._run, daemon=True, name="step-log-stream")

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        sep = _STEP_SEP.encode()
        seplen = len(sep)
        frame = 0               # chunk c is the agent's reaction to frame c
        kept = bytearray()      # content stored for the current chunk (<= budget)
        truncated = False
        scan = bytearray()      # recent bytes, to detect a separator across reads

        def absorb(b: bytes) -> None:
            nonlocal truncated
            if truncated:
                return
            room = self._budget - len(kept)
            if room <= 0:
                truncated = True
                return
            if len(b) <= room:
                kept.extend(b)
            else:
                kept.extend(b[:room])
                truncated = True

        def emit() -> None:
            nonlocal frame, kept, truncated
            text = kept.decode(errors="replace")
            if truncated:
                text += _TRUNCATION_NOTICE
            try:
                self._on_step_log(frame, text)
            except Exception:
                log.warning("on_step_log callback failed", exc_info=True)
            frame += 1
            kept = bytearray()
            truncated = False

        try:
            for data in self._container.logs(stream=True, follow=True):
                if not data:
                    continue
                scan.extend(data)
                # Pull off every complete <content><SEP> segment.
                while True:
                    i = scan.find(sep)
                    if i == -1:
                        break
                    absorb(bytes(scan[:i]))
                    emit()
                    del scan[: i + seplen]
                # Absorb all but the trailing seplen-1 bytes, which might be the
                # start of a separator that completes in the next read.
                if len(scan) > seplen - 1:
                    cut = len(scan) - (seplen - 1)
                    absorb(bytes(scan[:cut]))
                    del scan[:cut]
        except Exception:
            log.warning("step log streamer stopped", exc_info=True)


@dataclass
class AgentSpec:
    image: str
    name: str           # used as DNS name on the agent's private network
    

def run_match(
    sim_image: str,
    agents: list[AgentSpec],
    sim_args: SimArgs,
    *,
    router: Router,
    d_client: DockerClient,
    match_id: str | None = None,
    agent_mem_limit: str = "512m",
    agent_cpus: float = 1.0,
    agent_pids_limit: int = 128,
    grpc_ready_timeout_s: int = 2,
    extra_observers: list[ILoopObserver] | None = None,
    on_step_log: Callable[[int, str], None] | None = None,
    on_result: Callable[[MatchResult], None] | None = None,
) -> MatchResult:
    match_id = match_id or f"match-{uuid.uuid4().hex[:8]}"
    networks: list[Network] = []
    agent_containers: list[Container] = []
    sim_container: Container | None = None

    def _finish(result: MatchResult) -> MatchResult:
        # Fire on_result before the `finally` teardown so the caller can publish
        # the outcome immediately — Docker network/container teardown is slow
        # (~seconds) and shouldn't sit on the user-visible critical path.
        if on_result is not None:
            try:
                on_result(result)
            except Exception:
                log.warning("on_result callback failed", exc_info=True)
        return result

    def _init_failure(error: str) -> MatchResult:
        agent_logs = _collect_agent_logs(agent_containers)
        dev_step_logs = (
            _dev_step_logs(agent_logs.get(agent_containers[0].name, ""))
            if agent_containers else None
        )
        return _finish(MatchResult(
            success=False,
            agent_logs=agent_logs,
            dev_agent_step_logs=dev_step_logs,
            error=error,
        ))


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
                return _finish(MatchResult(
                    success=False,
                    error=f"agent image not found: {agent.image}",
                ))

        log.info("waiting for agents to be ready")
        if not _wait_for_agents_ready(agent_containers, timeout=grpc_ready_timeout_s):
            return _init_failure("agents not ready within timeout")

        # Fast-fail: an agent that crashed in its constructor was "running" during
        # the readiness poll but exits right after. Re-check before paying the
        # cost of starting the sim, so the failure (with its crash log) surfaces
        # immediately instead of after a pointless empty match.
        for c in agent_containers:
            try:
                c.reload()
            except (NotFound, APIError):
                return _init_failure("agent exited during startup")
            if c.status in ("exited", "dead"):
                log.info("agent %s crashed during startup — failing fast", c.name)
                return _init_failure("agent exited during startup")

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

        # Stream the dev agent's (seat 0) stdout per step to the caller, live.
        # follow=True starts from container creation, so no early output is missed.
        if on_step_log is not None and agent_containers:
            _StepLogStreamer(agent_containers[0], on_step_log).start()

        sim_net = d_client.networks.create(f"{match_id}-sim", driver="bridge", internal=False)
        networks.append(sim_net)
        router.attach(sim_net)
        loop_observable = SocketObservable()
        observable_addr = router.address_for(sim_net, loop_observable.port)

        full_sim_args = [
            "compute",
            "--ext-targets", *targets,
            "--ext-conn-timeout", "0.05",   # time to ESTABLISH the gRPC channel (agent boot)
            "--ext-init-timeout", "0.05",  # per-call deadline once connected (50ms)
            # "--decision-timeout-ms", "0", # this is enforced by the AgentContainerManager
            "--no-render",
            "--no-record",
            "--snake-count", "0",  # don't run any inproc snakes
            "--socket-observer", observable_addr,
            "--log-dir", "/tmp",
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
            detach=True,
            remove=False,
            mem_limit="2g",
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

        # Dev-agent (seat 0) console logs: per-step if it ran, else the whole
        # log (e.g. a crash during init, before any update).
        dev_step_logs = (
            _dev_step_logs(agent_logs.get(agent_containers[0].name, ""))
            if agent_containers else None
        )

        return _finish(MatchResult(
            success=exit_code == 0,
            sim_logs=sim_logs,
            agent_logs=agent_logs,
            tags_to_names=target_to_name,
            dev_agent_step_logs=dev_step_logs,
        ))

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
    