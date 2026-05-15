# services/runner/runner/cpu_budget_observer.py
import logging
import threading
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
    last_cpu_ns: int = 0       # cpu reading at the last step boundary
    step_budget_ns: int = 0    # cpu allowed since that boundary
    killed: bool = False


class CpuBudgetObserver(ILoopObserver):
    """
    Enforces a per-step CPU budget per agent. At each step boundary
    (notify_start, then each notify_step), the tracker records the agent's
    current CPU usage and grants them `per_step_budget` CPU-seconds before
    the next step boundary. A background thread polls cgroup cpu.stat
    between step boundaries — if an agent exceeds its allowance while the
    sim is still waiting for it, the container is killed.

    The first step gets `initial_budget + per_step_budget` to cover startup
    costs (imports, gRPC ready, first move).
    """

    def __init__(
        self,
        snake_name_to_container: dict[str, Container],
        per_step_budget_seconds: float = 0.1,
        initial_budget_seconds: float = 1.0,
        poll_interval_s: float = 0.01,
    ):
        super().__init__()
        self._name_to_container = snake_name_to_container
        self._per_step_budget_ns = int(per_step_budget_seconds * 1e9)
        self._initial_extra_ns = int(initial_budget_seconds * 1e9)
        self._poll_interval_s = poll_interval_s

        self._trackers: dict[int, _AgentTracker] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def notify_start(self, data: LoopStartData):
        names = data.env_meta_data.snake_tags
        with self._lock:
            for snake_id, name in names.items():
                container = self._name_to_container.get(name)
                if container is None:
                    log.debug("no container for snake %s (%s), skipping cpu budget", snake_id, name)
                    continue
                baseline = self._read_cpu_ns(container)
                self._trackers[snake_id] = _AgentTracker(
                    container=container,
                    name=name,
                    last_cpu_ns=baseline,
                    # first step gets extra time for startup
                    step_budget_ns=self._per_step_budget_ns + self._initial_extra_ns,
                )
                log.info("tracking cpu for snake %s (%s)", snake_id, name)

        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="cpu-budget")
        self._thread.start()

    def notify_step(self, data: LoopStepData):
        # step boundary: reset the per-step budget for each still-alive agent
        with self._lock:
            for snake_id, tracker in self._trackers.items():
                if tracker.killed:
                    continue
                if not data.alive_states.get(snake_id, False):
                    continue
                # snapshot where they are now; they get per_step_budget before next step
                current = self._read_cpu_ns(tracker.container)
                if current < 0:
                    tracker.killed = True
                    continue
                tracker.last_cpu_ns = current
                tracker.step_budget_ns = self._per_step_budget_ns

    def notify_stop(self, data: LoopStopData):
        self._shutdown()

    def reset(self):
        self._shutdown()
        with self._lock:
            self._trackers.clear()

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
                        tracker.killed = True
                        continue

                    used_this_step = current - tracker.last_cpu_ns
                    if used_this_step > tracker.step_budget_ns:
                        log.warning(
                            "snake %s (%s) over per-step cpu budget: used %.3fs / %.3fs -> killing",
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