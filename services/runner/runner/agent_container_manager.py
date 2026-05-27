# services/runner/runner/agent_container_manager.py
import logging
import threading
import time
from pathlib import Path
from dataclasses import dataclass, field
from typing import Callable

from docker.errors import APIError, NotFound
from docker.models.containers import Container

from snake_sim.environment.interfaces.loop_observer_interface import ILoopObserver
from snake_sim.environment.types import LoopStartData, LoopStepData, LoopStopData

log = logging.getLogger(__name__)

_CGROUP_BASE = Path("/sys/fs/cgroup")


@dataclass
class _AgentTracker:
    container: Container
    name: str
    last_cpu_ns: int = 0
    step_budget_ns: int = 0
    # Running bank for the accumulating budget (ns). Refilled by
    # `accumulating_step_ns` and debited by per-step CPU usage at every
    # notify_step. < 0 → agent has burned more than the long-run rate
    # allows and is killed.
    accumulating_remaining_ns: int = 0
    # True from set_agent_containers until notify_start fires for this seat.
    # Used so the poll loop can distinguish a startup-phase CPU kill from a
    # per-step CPU kill — the budget is `startup_budget_ns` in either case
    # but the human-readable reason is different.
    in_startup: bool = True
    killed: bool = False
    # One of: None (alive / clean exit), "startup_cpu", "per_step",
    # "sustained", "init_failure", "dead". Used so the runner can explain a
    # kill to the dev agent's console.
    kill_reason: str | None = None


class AgentContainerManager(ILoopObserver):
    """
    Enforces CPU budgets for agent containers across all phases of a match.

    Phase 1 — startup (set_agent_containers → notify_start): covers the
    container's constructor and all gRPC init calls. Budget:
    startup_budget_seconds of CPU time. The gRPC init_timeout on the sim
    side prevents wall-clock hangs; this catches CPU-burning attacks.

    Phase 2 — per-step (notify_start onward): each notify_step boundary resets
    the clock. Budget: per_step_budget_seconds per step, plus
    initial_budget_seconds extra on the very first step.

    Phase 2 also runs an *accumulating* budget when
    `accumulating_step_seconds` > 0. Each agent gets a bank that starts at
    `accumulating_initial_seconds` (defaults to per_step_budget_seconds — one
    fully-slow step is OK), grows by `accumulating_step_seconds` every
    notify_step, and is debited by that step's CPU usage. The bank is capped
    at `accumulating_max_seconds`. If the bank goes negative the agent is
    killed — this catches agents that stay under the per-step cap but average
    above the long-run rate.

    Containers are also killed as soon as their snake is marked dead by the
    sim (alive_states false in notify_step, or absent from notify_start
    because it was dropped during init).
    """

    def __init__(
        self,
        snake_name_to_container: dict[str, Container],
        per_step_budget_seconds: float = 0.1,
        initial_budget_seconds: float = 1.0,
        startup_budget_seconds: float = 5.0,
        accumulating_step_seconds: float = 0.0,
        accumulating_initial_seconds: float | None = None,
        accumulating_max_seconds: float = 0.5,
        poll_interval_s: float = 0.01,
        on_exec_times: Callable[[int, dict[int, float]], None] | None = None,
    ):
        super().__init__()
        self._name_to_container = snake_name_to_container
        self._per_step_budget_ns = int(per_step_budget_seconds * 1e9)
        self._initial_extra_ns = int(initial_budget_seconds * 1e9)
        self._startup_budget_ns = int(startup_budget_seconds * 1e9)
        self._accumulating_step_ns = int(accumulating_step_seconds * 1e9)
        # Initial bank defaults to per_step_budget — one fully-slow step is OK.
        if accumulating_initial_seconds is None:
            accumulating_initial_seconds = per_step_budget_seconds
        self._accumulating_initial_ns = int(accumulating_initial_seconds * 1e9)
        self._accumulating_max_ns = int(accumulating_max_seconds * 1e9)
        self._accumulating_enabled = self._accumulating_step_ns > 0
        self._poll_interval_s = poll_interval_s
        self._on_exec_times = on_exec_times

        self._trackers: dict[int, _AgentTracker] = {}
        self._exec_times_ms: dict[int, list[float]] = {}  # snake_id → [ms per step]
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._seat_to_container: dict[int, Container] = {}

    def set_agent_containers(self, seat_to_container: dict[int, Container]) -> None:
        """Start startup-phase monitoring as soon as containers are ready."""
        self._seat_to_container = seat_to_container

        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._stop_event.clear()

        with self._lock:
            self._trackers.clear()
            for seat, container in seat_to_container.items():
                baseline = self._read_cpu_ns(container)
                self._trackers[seat] = _AgentTracker(
                    container=container,
                    name=f"seat-{seat}",  # updated to real name in notify_start
                    last_cpu_ns=max(baseline, 0),
                    step_budget_ns=self._startup_budget_ns,
                )
                log.info("cpu startup monitoring: seat %d (budget %.1fs)", seat, self._startup_budget_ns / 1e9)

        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="cpu-budget")
        self._thread.start()

    def notify_start(self, data: LoopStartData):
        names = data.env_meta_data.snake_tags  # snake_id -> name, only live snakes
        with self._lock:
            active_ids = set(names.keys())
            for snake_id, name in names.items():
                container = self._seat_to_container.get(snake_id) or self._name_to_container.get(name)
                if container is None:
                    log.debug("no container for snake %s (%s), skipping cpu budget", snake_id, name)
                    continue
                baseline = self._read_cpu_ns(container)
                if snake_id in self._trackers:
                    tracker = self._trackers[snake_id]
                    tracker.name = name
                    tracker.last_cpu_ns = max(baseline, 0)
                    tracker.step_budget_ns = self._per_step_budget_ns + self._initial_extra_ns
                    tracker.accumulating_remaining_ns = self._accumulating_initial_ns
                    tracker.in_startup = False
                else:
                    self._trackers[snake_id] = _AgentTracker(
                        container=container,
                        name=name,
                        last_cpu_ns=max(baseline, 0),
                        step_budget_ns=self._per_step_budget_ns + self._initial_extra_ns,
                        accumulating_remaining_ns=self._accumulating_initial_ns,
                        in_startup=False,
                    )
                log.info("cpu per-step monitoring: snake %s (%s)", snake_id, name)

            # Seats that didn't make it into the match were dropped during init
            # (e.g. gRPC init timeout). Kill their containers now.
            for seat, tracker in self._trackers.items():
                if seat not in active_ids and not tracker.killed:
                    log.info("seat %d absent from match start (init failure) — killing container", seat)
                    tracker.killed = True
                    tracker.kill_reason = "init_failure"
                    self._kill_container(tracker.container)

        if self._thread is None or not self._thread.is_alive():
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="cpu-budget")
            self._thread.start()

    def notify_step(self, data: LoopStepData):
        step_times: dict[int, float] = {}
        with self._lock:
            for snake_id, tracker in self._trackers.items():
                if tracker.killed:
                    continue
                current = self._read_cpu_ns(tracker.container)
                if current < 0:
                    continue
                delta_ns = max(0, current - tracker.last_cpu_ns)
                step_times[snake_id] = delta_ns / 1e6
                self._exec_times_ms.setdefault(snake_id, []).append(delta_ns / 1e6)
                tracker.last_cpu_ns = current
                tracker.step_budget_ns = self._per_step_budget_ns

                if self._accumulating_enabled:
                    tracker.accumulating_remaining_ns += self._accumulating_step_ns - delta_ns
                    if tracker.accumulating_remaining_ns > self._accumulating_max_ns:
                        tracker.accumulating_remaining_ns = self._accumulating_max_ns
                    if tracker.accumulating_remaining_ns < 0:
                        log.warning(
                            "snake %s (%s) over accumulating cpu budget "
                            "(bank %.3fs, used %.3fs this step) -> killing",
                            snake_id, tracker.name,
                            tracker.accumulating_remaining_ns / 1e9,
                            delta_ns / 1e9,
                        )
                        tracker.killed = True
                        tracker.kill_reason = "sustained"
                        self._kill_container(tracker.container)
                        continue

                if not data.alive_states.get(snake_id, False):
                    log.info("snake %s (%s) is dead — killing container", snake_id, tracker.name)
                    tracker.killed = True
                    tracker.kill_reason = "dead"
                    self._kill_container(tracker.container)
        if step_times and self._on_exec_times is not None:
            try:
                self._on_exec_times(data.step, step_times)
            except Exception:
                log.warning("on_exec_times callback failed", exc_info=True)

    def get_exec_times(self) -> dict[int, list[float]]:
        """Return accumulated per-step CPU times (ms) keyed by snake_id."""
        with self._lock:
            return {k: list(v) for k, v in self._exec_times_ms.items()}

    def get_kill_reason(self, seat: int) -> str | None:
        """Why the given seat's container was killed, if it was. One of
        "per_step", "sustained", "init_failure", "dead", or None if the agent
        ran to clean completion / is still alive."""
        with self._lock:
            tracker = self._trackers.get(seat)
            return tracker.kill_reason if tracker else None

    def get_budgets(self) -> dict[str, float]:
        """Return the budget config as seconds, suitable for bundle metadata."""
        return {
            "per_step_seconds": self._per_step_budget_ns / 1e9,
            "initial_seconds": self._initial_extra_ns / 1e9,
            "startup_seconds": self._startup_budget_ns / 1e9,
            "accumulating_step_seconds": self._accumulating_step_ns / 1e9,
            "accumulating_initial_seconds": self._accumulating_initial_ns / 1e9,
            "accumulating_max_seconds": self._accumulating_max_ns / 1e9,
        }

    def notify_stop(self, data: LoopStopData):
        self._shutdown()

    def reset(self):
        self._shutdown()
        with self._lock:
            self._trackers.clear()
            self._seat_to_container.clear()

    def close(self):
        self._shutdown()

    def _shutdown(self):
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def _poll_loop(self):
        log.info("cpu budget poll loop started")
        while not self._stop_event.is_set():
            with self._lock:
                for snake_id, tracker in self._trackers.items():
                    if tracker.killed:
                        continue

                    current = self._read_cpu_ns(tracker.container)
                    if current < 0:
                        continue

                    used_this_step = current - tracker.last_cpu_ns
                    if used_this_step > tracker.step_budget_ns:
                        log.warning(
                            "snake %s (%s) over cpu budget: used %.3fs / %.3fs -> killing",
                            snake_id, tracker.name,
                            used_this_step / 1e9, tracker.step_budget_ns / 1e9,
                        )
                        tracker.killed = True
                        tracker.kill_reason = "startup_cpu" if tracker.in_startup else "per_step"
                        self._kill_container(tracker.container)

            self._stop_event.wait(self._poll_interval_s)
        log.info("cpu budget poll loop stopped")

    @staticmethod
    def _read_cpu_ns(container: Container) -> int:
        container_id = container.id
        if container_id is None:
            return -1

        candidates = [
            _CGROUP_BASE / "system.slice" / f"docker-{container_id}.scope" / "cpu.stat",
            _CGROUP_BASE / f"docker/{container_id}/cpu.stat",
        ]

        for path in candidates:
            if not path.exists():
                continue
            try:
                content = path.read_text()
            except OSError:
                return -1

            for line in content.splitlines():
                if line.startswith("usage_usec"):
                    try:
                        usec = int(line.split()[1])
                        return usec * 1000
                    except (ValueError, IndexError):
                        return -1
            return -1

        return -1

    @staticmethod
    def _kill_container(container: Container) -> None:
        try:
            container.kill()
        except NotFound:
            pass  # already gone — goal state met
        except APIError as e:
            # 409 Conflict = "container is not running": it already exited, which
            # is exactly what we wanted. Anything else is genuinely unexpected.
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status == 409:
                log.debug("container %s already stopped", container.name)
            else:
                log.warning("failed to kill container %s: %s", container.name, e)
