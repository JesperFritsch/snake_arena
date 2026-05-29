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
_STEP_STDOUT_BUDGET = 10_000     # bytes per step chunk before truncation notice
_STARTUP_LOG_BUDGET = 16_000    # bytes kept when an agent crashes before any step
_TRUNCATION_NOTICE = "\n[stdout truncated — output too long for this step]\n"
_MATCH_LOG_CAP = 10 * 1024 * 1024  # 10 MiB per agent kept for the bundle
_MATCH_LOG_TRUNCATION_BANNER = (
    "\n"
    "=========================================================\n"
    "LOG TRUNCATED: this agent produced more than 10 MiB of\n"
    "stdout/stderr during the match. Output past this point\n"
    "was dropped. Reduce log volume to see your full output.\n"
    "=========================================================\n"
)


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


def _budget_kill_note(kill_reason: str | None, budgets: dict[str, float]) -> str | None:
    """Human-readable banner explaining why the dev agent was killed,
    prepended to the dev agent's last step log. Returns None for kills that
    don't need a banner (clean exit, snake-died-in-game).

    `budgets` is the dict from AgentContainerManager.get_budgets(); every
    key referenced below is unconditionally written by that producer.
    """
    if kill_reason == "per_step":
        ms = budgets["per_step_seconds"] * 1000
        return (
            f"=== Agent killed: exceeded the per-step CPU budget "
            f"(~{ms:.0f} ms in one step). ===\n"
        )
    if kill_reason == "sustained":
        step_ms = budgets["accumulating_step_seconds"] * 1000
        max_ms = budgets["accumulating_max_seconds"] * 1000
        return (
            f"=== Agent killed: exceeded the sustained CPU budget "
            f"(long-run average above {step_ms:.0f} ms/step; up to "
            f"{max_ms:.0f} ms of saved credit allowed). The agent stayed under "
            f"the per-step cap but its average over time was too high. ===\n"
        )
    if kill_reason == "wall_clock":
        return (
            "=== Agent killed: did not respond within the per-step wall-clock "
            "budget while using essentially no CPU. The agent appears to be "
            "sleeping or blocked on I/O instead of computing — don't sleep. ===\n"
        )
    if kill_reason == "sustained_wall":
        step_ms = budgets["wall_accumulating_step_seconds"] * 1000
        max_ms = budgets["wall_accumulating_max_seconds"] * 1000
        return (
            f"=== Agent killed: exceeded the sustained wall-clock budget "
            f"(non-CPU wall time grew faster than {step_ms:.0f} ms/step; "
            f"up to {max_ms:.0f} ms of saved credit allowed). The agent kept "
            f"sleeping just under the per-step wall-clock limit. ===\n"
        )
    if kill_reason == "startup_cpu":
        ms = budgets["startup_seconds"] * 1000
        return (
            f"=== Agent killed during startup: exceeded the startup CPU "
            f"budget (~{ms:.0f} ms). Agent constructor + gRPC init must "
            f"finish within this window. ===\n"
        )
    if kill_reason == "init_failure":
        return (
            "=== Agent killed during startup: did not respond to the sim's "
            "gRPC init within the wall-clock timeout. The agent either "
            "crashed before connecting or took too long to be ready. ===\n"
        )
    return None


def _dev_step_logs(dev_text: str | None) -> list[str] | None:
    """Dev-agent (seat 0) console logs. If the agent took steps, split into
    per-frame chunks. If it crashed before the first update (no separators),
    return the whole log as a single step-0 entry so the console still shows
    what happened. None when the agent produced no logs at all."""
    if dev_text is None or not dev_text.strip():
        return None
    if _STEP_SEP in dev_text:
        return _split_step_logs(dev_text)
    return [_tail_log(dev_text)]


class _StepLogStreamer:
    """Follows a container's stdout in a daemon thread. Two outputs:

    1. Per-step live stream via on_step_log(frame, text) as each frame's
       output completes (delimited by the harness separator). Each chunk is
       capped at `budget` bytes mid-stream. Disabled when on_step_log=None.
    2. Full accumulated log (capped at _MATCH_LOG_CAP bytes) exposed via
       get_full_log(). Unlike docker's default rotation — which keeps the
       *last* N bytes — we keep the *first* 10 MiB (where startup and
       first-error usually live) and drop anything past it. If truncation
       happened, the returned text gets a clear banner appended so the
       user sees they produced too much output.
    """

    def __init__(
        self,
        container: Container,
        on_step_log: Callable[[int, str], None] | None = None,
        budget: int = _STEP_STDOUT_BUDGET,
    ) -> None:
        self._container = container
        self._on_step_log = on_step_log
        self._budget = budget
        self._full_log = bytearray()
        self._full_log_truncated = False
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True, name="step-log-stream")

    def start(self) -> None:
        self._thread.start()

    @property
    def container(self) -> Container:
        return self._container

    def get_full_log(self) -> str:
        """Snapshot of the agent's stdout so far, with a truncation banner
        appended if the 10 MiB cap was hit. Safe to call while the
        streamer is still running."""
        with self._lock:
            text = bytes(self._full_log).decode(errors="replace")
            truncated = self._full_log_truncated
        if truncated:
            text += _MATCH_LOG_TRUNCATION_BANNER
        return text

    def _append_full(self, data: bytes) -> None:
        # Keep the FIRST _MATCH_LOG_CAP bytes; drop the rest. Called from
        # the streaming thread; lock against concurrent get_full_log().
        with self._lock:
            if self._full_log_truncated:
                return
            room = _MATCH_LOG_CAP - len(self._full_log)
            if room <= 0:
                self._full_log_truncated = True
            elif len(data) <= room:
                self._full_log.extend(data)
            else:
                self._full_log.extend(data[:room])
                self._full_log_truncated = True

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
                self._append_full(data)
                if self._on_step_log is None:
                    continue  # accumulate-only mode (non-dev agents)
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
    match_id: str,
    per_step_budget_seconds: float,        # per-step CPU budget enforced by the cgroup observer
    agent_mem_limit: str = "512m",
    agent_cpus: float = 1.0,
    agent_pids_limit: int = 128,
    grpc_ready_timeout_s: int = 2,
    extra_observers: list[ILoopObserver] | None = None,
    on_step_log: Callable[[int, str], None] | None = None,
    on_exec_times: Callable[[int, dict[int, float]], None] | None = None,
    on_result: Callable[[MatchResult], None] | None = None,
) -> MatchResult:
    networks: list[Network] = []
    agent_containers: list[Container] = []
    log_streamers: list[_StepLogStreamer] = []
    sim_container: Container | None = None
    loop_observable: ILoopObservable | None = None

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
        agent_logs = _collect_agent_logs(agent_containers, log_streamers)
        dev_step_logs = (
            _dev_step_logs(agent_logs.get(agent_containers[0].name))
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
                    # Bound the FDs an agent can hold open — there's no
                    # legitimate reason for an agent to keep many files /
                    # sockets, and an unbounded default lets a misbehaving
                    # agent stall its own gRPC accept loop (which surfaces
                    # as a misleading "init_failure" kill reason).
                    ulimits=[docker.types.Ulimit(name="nofile", soft=128, hard=128)],
                    # Cap on-disk log buffering at the docker daemon. The
                    # runner streams stdout live via _StepLogStreamer and
                    # re-reads the full log once at match end; both can
                    # tolerate truncation of stdout-spam attacks. The cap
                    # rotates in-place (max-file=1) so total disk use per
                    # container is bounded to ~max-size and the file is
                    # destroyed when the container is removed at cleanup.
                    log_config=docker.types.LogConfig(
                        type=docker.types.LogConfig.types.JSON,
                        config={"max-size": "10m", "max-file": "1"},
                    ),
                    cap_drop=["ALL"],
                    security_opt=["no-new-privileges"],
                    user="1000:1000",
                )
                agent_containers.append(container)
                target_to_container[target] = container
                target_to_name[target] = agent.name
                # Start the log streamer immediately so it captures stdout
                # from container start, including init crashes. Only the
                # dev agent (the first one) forwards per-step chunks to
                # on_step_log for the live console; the rest accumulate
                # silently for the post-match bundle.
                streamer = _StepLogStreamer(
                    container,
                    on_step_log=on_step_log if len(agent_containers) == 1 else None,
                )
                streamer.start()
                log_streamers.append(streamer)
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

        def _disconnect_sim_from_seat(seat_id: int) -> None:
            # Fired by AgentContainerManager just before it kills a snake's
            # container. `container.kill()` is async — docker daemon →
            # containerd → SIGKILL delivery adds ~10–50 ms of latency, and
            # in that window the sim is happily exchanging gRPC frames
            # with the (still-alive) snake. Disconnecting the sim from
            # this snake's private network breaks that TCP connection
            # right away, so the sim's next request fails with
            # ConnectionError instead of getting a valid (and now-banned)
            # decision back.
            if sim_container is None:
                return  # sim not started yet — pre-match init_failure path
            if seat_id >= len(agent_containers):
                return
            net = networks[seat_id]
            try:
                net.disconnect(sim_container, force=True)
            except (NotFound, APIError) as e:
                log.debug(
                    "disconnect sim from seat %d network failed: %s",
                    seat_id, e,
                )

        cpu_observer = AgentContainerManager(
            snake_name_to_container=target_to_container,
            per_step_budget_seconds=per_step_budget_seconds,
            initial_budget_seconds=0.2,
            startup_budget_seconds=0.2,
            # CPU accumulating long-run rate: bank grows 10ms/step (vs 50ms
            # per-step cap), starts at per_step_budget, capped at 500ms.
            # Catches agents that stay under the per-step cap but average
            # above the long-run rate.
            accumulating_step_seconds=per_step_budget_seconds / 5,
            accumulating_max_seconds=per_step_budget_seconds * 10,
            # Per-step wall-clock guard: catches agents that block on sleep
            # / I/O. The effective budget is computed adaptively from
            # observed contention each poll iteration (see manager docstring).
            # Defaults: safety×3, hard floor 1s.
            # Sustained-wall budget: bounds long-run "sleep just under the
            # per-step threshold" abuse. Bank grows 50ms/step, starts at
            # 500ms, capped at 1s. Strict — a 1.4s/step sleeper drains it
            # in a single step.
            wall_accumulating_step_seconds=per_step_budget_seconds / 2,
            wall_accumulating_initial_seconds=per_step_budget_seconds * 10,
            wall_accumulating_max_seconds=per_step_budget_seconds * 20,
            poll_interval_s=0.01,
            on_exec_times=on_exec_times,
            on_seat_killed=_disconnect_sim_from_seat,
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
            "--ext-conn-timeout", "1.0",    # time to ESTABLISH the gRPC channel (agent boot)
            "--ext-init-timeout", "0.05",   # per-call deadline once connected
            "--decision-timeout-ms", "0", # this is enforced by the AgentContainerManager
            "--no-render",
            "--no-record",
            "--snake-count", "0",  # don't run any inproc snakes
            "--socket-observer", observable_addr,
            "--log-dir", "/tmp",
            "--log-level", "DEBUG",
            *sim_args.to_args(),
        ]

        loop_observable.add_observer(cpu_observer)
        for obs in (extra_observers or []):
            loop_observable.add_observer(obs)
        loop_observable.start()

        log.info("starting sim with args: %s", full_sim_args)

        # Create the sim container without starting it, connect it to all agent
        # networks first, then start — avoids a race where the sim resolves
        # agent hostnames before Docker has wired up the network routes.
        sim_container = d_client.containers.create(
            sim_image,
            command=full_sim_args,
            network=sim_net.name,
            name=f"{match_id}-sim",
            mem_limit="2g",
        )
        for net in networks[:-1]:
            net.connect(sim_container)
        sim_container.start()

        try:
            ex_result = sim_container.wait()
            exit_code = ex_result["StatusCode"]
        except Exception as e:
            log.warning("sim wait failed: %s", e)
            try:
                sim_container.kill()
            except (NotFound, APIError) as kill_err:
                log.warning("failed to kill sim container: %s", kill_err)
            exit_code = -1

        sim_logs = sim_container.logs().decode(errors="replace")
        agent_logs = _collect_agent_logs(agent_containers, log_streamers)

        # Dev-agent (seat 0) console logs: per-step if it ran, else the whole
        # log (e.g. a crash during init, before any update).
        dev_step_logs = (
            _dev_step_logs(agent_logs.get(agent_containers[0].name))
            if agent_containers else None
        )
        # If the dev agent was killed by a budget violation, surface that to
        # the console so the user doesn't read an empty log and assume their
        # code crashed for unknown reasons. The banner must land at the step
        # *where the kill happened*, not at the last step the harness emitted
        # a separator for — a sleeper that never reaches its first print
        # would otherwise put the banner on the previous step.
        if agent_containers:
            note = _budget_kill_note(cpu_observer.get_kill_reason(0), cpu_observer.get_budgets())
            if note:
                # Land the banner *at or after* the last step the agent
                # actually emitted output for. Two cases the max() handles:
                #   – manager processed N notify_steps but the agent ran
                #     M > N steps before `container.kill()` actually took
                #     effect (docker-kill latency, see match.py:Fix-B).
                #     Anchor on M so the banner doesn't land mid-log with
                #     real "step!" output dangling after it.
                #   – agent died before printing as many steps as the
                #     manager saw (M < N). Pad with empty chunks so the
                #     banner still lands at the manager's view of the
                #     kill step (consistent with replay frame indexing).
                if dev_step_logs is None:
                    dev_step_logs = []
                kill_step = max(
                    len(cpu_observer.get_exec_times().get(0, [])),
                    len(dev_step_logs),
                )
                while len(dev_step_logs) < kill_step:
                    dev_step_logs.append("")
                dev_step_logs.append(note)
        kill_reasons = {
            seat: cpu_observer.get_kill_reason(seat)
            for seat in range(len(agents))
        }
        return _finish(MatchResult(
            success=exit_code == 0,
            sim_logs=sim_logs,
            agent_logs=agent_logs,
            tags_to_names=target_to_name,
            dev_agent_step_logs=dev_step_logs,
            exec_times=cpu_observer.get_exec_times(),
            wall_step_times=cpu_observer.get_wall_step_times(),
            budgets=cpu_observer.get_budgets(),
            kill_reasons=kill_reasons,
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


def _collect_agent_logs(
    containers: list[Container],
    streamers: list["_StepLogStreamer"] | None = None,
) -> dict[str, str]:
    """Per-container stdout, source-of-truth from our in-process streamer
    (caps at 10 MiB, banner-appended on overflow). Falls back to docker's
    own log API for any container without a streamer — only the
    pre-streamer-start failure paths use the fallback."""
    streamer_by_container = {id(s.container): s for s in (streamers or [])}
    logs: dict[str, str] = {}
    for c in containers:
        s = streamer_by_container.get(id(c))
        if s is not None:
            logs[c.name] = s.get_full_log()
            continue
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
    