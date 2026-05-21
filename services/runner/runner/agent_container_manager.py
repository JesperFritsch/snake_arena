# services/runner/runner/agent_container_manager.py
import logging
import threading
import time
from pathlib import Path
from dataclasses import dataclass

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
    killed: bool = False


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
        poll_interval_s: float = 0.01,
    ):
        super().__init__()
        self._name_to_container = snake_name_to_container
        self._per_step_budget_ns = int(per_step_budget_seconds * 1e9)
        self._initial_extra_ns = int(initial_budget_seconds * 1e9)
        self._startup_budget_ns = int(startup_budget_seconds * 1e9)
        self._poll_interval_s = poll_interval_s

        self._trackers: dict[int, _AgentTracker] = {}
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
                else:
                    self._trackers[snake_id] = _AgentTracker(
                        container=container,
                        name=name,
                        last_cpu_ns=max(baseline, 0),
                        step_budget_ns=self._per_step_budget_ns + self._initial_extra_ns,
                    )
                log.info("cpu per-step monitoring: snake %s (%s)", snake_id, name)

            # Seats that didn't make it into the match were dropped during init
            # (e.g. gRPC init timeout). Kill their containers now.
            for seat, tracker in self._trackers.items():
                if seat not in active_ids and not tracker.killed:
                    log.info("seat %d absent from match start (init failure) — killing container", seat)
                    tracker.killed = True
                    self._kill_container(tracker.container)

        if self._thread is None or not self._thread.is_alive():
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="cpu-budget")
            self._thread.start()

    def notify_step(self, data: LoopStepData):
        with self._lock:
            for snake_id, tracker in self._trackers.items():
                if tracker.killed:
                    continue
                if not data.alive_states.get(snake_id, False):
                    log.info("snake %s (%s) is dead — killing container", snake_id, tracker.name)
                    tracker.killed = True
                    self._kill_container(tracker.container)
                    continue
                current = self._read_cpu_ns(tracker.container)
                if current < 0:
                    continue
                tracker.last_cpu_ns = current
                tracker.step_budget_ns = self._per_step_budget_ns

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
        except (APIError, NotFound) as e:
            log.warning("failed to kill container %s: %s", container.name, e)
