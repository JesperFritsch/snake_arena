# services/runner/runner/agent_container_manager.py
import logging
import threading
import time
from collections import deque
from pathlib import Path
from dataclasses import dataclass, field
from typing import Callable, Deque

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
    # Running bank for the CPU accumulating budget (ns). Refilled by
    # `accumulating_step_ns` and debited by per-step CPU usage at every
    # notify_step. < 0 → agent has burned more than the long-run rate
    # allows and is killed.
    accumulating_remaining_ns: int = 0
    # Running bank for the wall-clock accumulating budget (ns). Refilled by
    # `wall_accumulating_step_ns` and debited by *excess* wall (wall_step
    # minus the part explained by cpu_step under current contention). Bounds
    # how much "sleeping just under the per-step wall threshold" an agent
    # can do across the match.
    wall_bank_remaining_ns: int = 0
    # Wall-clock and CPU snapshot at the start of the current step. The poll
    # loop uses these to catch agents that are sleeping / hanging — they
    # blow the per-step wall-clock budget without ever burning CPU.
    step_start_wall_ns: int = 0
    step_start_cpu_ns: int = 0
    # True from set_agent_containers until notify_start fires for this seat.
    # Used so the poll loop can distinguish a startup-phase CPU kill from a
    # per-step CPU kill — the budget is `startup_budget_ns` in either case
    # but the human-readable reason is different.
    in_startup: bool = True
    killed: bool = False
    # One of: None (alive / clean exit), "startup_cpu", "per_step",
    # "sustained", "wall_clock", "sustained_wall", "init_failure", "dead".
    # Used so the runner can explain a kill to the dev agent's console.
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

    Phase 2 also runs two wall-clock guards (enabled when
    `wall_clock_safety_factor` > 0):

    Per-step wall budget — if notify_step doesn't fire for longer than a
    contention-adjusted budget *and* the agent has used less than
    `wall_clock_sleep_threshold_seconds` of CPU in that window, the agent
    is presumed sleeping / blocked and killed. The budget is computed
    adaptively each poll iteration:
      1. Rolling mean of (wall_step / max_cpu_step) over the last K
         completed steps, when there are at least `wall_clock_min_history`
         steps whose max_cpu_step >= `wall_clock_contention_min_cpu_seconds`.
         The threshold filters out all-sleeper / all-trivial steps that would
         otherwise let an attacker's slow wall divided by its tiny cpu inflate
         "contention" enough to mask itself.
      2. Otherwise, PSI from `/proc/pressure/cpu` (or a configured cgroup
         path) scaled by `wall_clock_psi_scale_k`.
      3. Otherwise, `wall_clock_fallback_contention`.
    All three tiers are clamped to [1.0, `wall_clock_max_contention`]; the
    upper cap is a safety net against measurement pollution (real host
    contention almost never exceeds 5×). The final budget is
    `max(hard_floor, per_step_cpu_budget * contention * safety_factor)`.

    Sustained wall budget — bounds long-run "sleep just under the line"
    abuse. Each agent has a wall bank that refills by
    `wall_accumulating_step_seconds` per step and is debited by *excess
    wall*, defined as `max(0, wall_step - cpu_per_step * contention)`. A
    sleeping agent contributes ~0 CPU, so essentially all of wall_step is
    "excess" and drains the bank fast. An agent legitimately slowed by
    contention sees expected_wall ≈ wall_step, so excess is ~0. Bank is
    capped at `wall_accumulating_max_seconds`; when it goes negative the
    agent is killed.

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
        # Wall-clock guard (per step). Contention-adaptive. Set
        # safety_factor <= 0 to disable. Defaults give roughly:
        #   budget_idle  ≈ 50ms (cpu) * 1 (contention) * 3 (safety) = 150ms,
        #                  clamped up to hard_floor (1s).
        #   budget_5x    ≈ 50ms * 5 * 3 = 750ms, still ≥ hard_floor.
        wall_clock_safety_factor: float = 3.0,
        wall_clock_hard_floor_seconds: float = 1.0,
        wall_clock_sleep_threshold_seconds: float = 0.001,
        wall_clock_psi_path: str | None = "/proc/pressure/cpu",
        wall_clock_psi_scale_k: float = 1.0,
        wall_clock_fallback_contention: float = 5.0,
        wall_clock_min_history: int = 3,
        wall_clock_history_size: int = 8,
        # Cap on the contention factor. Without it, a sleeper's slow wall
        # divided by its tiny cpu produces an enormous "contention" reading
        # that the same agent then uses to justify its own slow wall — the
        # sustained-wall budget self-defeats. Real host contention almost
        # never exceeds ~5×.
        wall_clock_max_contention: float = 5.0,
        # Minimum max_cpu_per_step for a step to count toward the rolling
        # contention mean. Filters out steps where every agent was sleeping
        # or doing nothing — those tell us nothing about contention and
        # would otherwise pollute the measurement.
        wall_clock_contention_min_cpu_seconds: float = 0.005,
        # Wall-clock accumulating guard. Strict by default — caps long-run
        # "sleep just under the line" abuse without affecting honest agents.
        wall_accumulating_step_seconds: float = 0.0,
        wall_accumulating_initial_seconds: float = 0.5,
        wall_accumulating_max_seconds: float = 1.0,
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
        # Wall-clock guard config.
        self._wall_clock_safety_factor = wall_clock_safety_factor
        self._wall_clock_hard_floor_ns = int(wall_clock_hard_floor_seconds * 1e9)
        self._wall_clock_sleep_threshold_ns = int(wall_clock_sleep_threshold_seconds * 1e9)
        self._wall_clock_psi_path = Path(wall_clock_psi_path) if wall_clock_psi_path else None
        self._wall_clock_psi_scale_k = wall_clock_psi_scale_k
        self._wall_clock_fallback_contention = wall_clock_fallback_contention
        self._wall_clock_min_history = wall_clock_min_history
        self._wall_clock_max_contention = wall_clock_max_contention
        self._wall_clock_contention_min_cpu_ns = int(wall_clock_contention_min_cpu_seconds * 1e9)
        self._wall_clock_enabled = wall_clock_safety_factor > 0
        # Sustained-wall config.
        self._wall_accumulating_step_ns = int(wall_accumulating_step_seconds * 1e9)
        self._wall_accumulating_initial_ns = int(wall_accumulating_initial_seconds * 1e9)
        self._wall_accumulating_max_ns = int(wall_accumulating_max_seconds * 1e9)
        self._wall_accumulating_enabled = self._wall_accumulating_step_ns > 0
        # Rolling (wall_step_ns, max_cpu_step_ns) samples from completed
        # steps. Used by _contention_factor's tier 1.
        self._step_history: Deque[tuple[int, int]] = deque(maxlen=wall_clock_history_size)
        # Wall timestamp of the previous notify_step, for measuring wall_step.
        self._last_notify_step_wall_ns: int = 0
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
        now_wall = time.monotonic_ns()
        with self._lock:
            # Seed the step-history wall reference and clear stale samples
            # from a previous match.
            self._last_notify_step_wall_ns = now_wall
            self._step_history.clear()
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
                    tracker.wall_bank_remaining_ns = self._wall_accumulating_initial_ns
                    tracker.step_start_wall_ns = now_wall
                    tracker.step_start_cpu_ns = max(baseline, 0)
                    tracker.in_startup = False
                else:
                    self._trackers[snake_id] = _AgentTracker(
                        container=container,
                        name=name,
                        last_cpu_ns=max(baseline, 0),
                        step_budget_ns=self._per_step_budget_ns + self._initial_extra_ns,
                        accumulating_remaining_ns=self._accumulating_initial_ns,
                        wall_bank_remaining_ns=self._wall_accumulating_initial_ns,
                        step_start_wall_ns=now_wall,
                        step_start_cpu_ns=max(baseline, 0),
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
        now_wall = time.monotonic_ns()
        with self._lock:
            wall_step_ns = max(0, now_wall - self._last_notify_step_wall_ns) if self._last_notify_step_wall_ns else 0
            self._last_notify_step_wall_ns = now_wall
            # First pass: snapshot CPU delta for each live tracker. We need
            # max_cpu_step before debiting the wall bank, so this is a two-pass
            # loop.
            cpu_deltas: dict[int, int] = {}
            for snake_id, tracker in self._trackers.items():
                if tracker.killed:
                    continue
                current = self._read_cpu_ns(tracker.container)
                if current < 0:
                    continue
                delta_ns = max(0, current - tracker.last_cpu_ns)
                cpu_deltas[snake_id] = delta_ns
                # Record the cpu reading so the second pass and downstream
                # state updates use a consistent value for this step.
                tracker.last_cpu_ns = current
            max_cpu_step_ns = max(cpu_deltas.values(), default=0)
            # Push to step-history (used for contention tier 1) only if we
            # had at least one agent doing real work — a stalled step
            # (all cpu deltas ≈ noise) tells us nothing about contention and
            # would otherwise let a sleeper inflate the measured contention
            # to justify its own slow wall (sustained-wall self-defeat).
            if wall_step_ns > 0 and max_cpu_step_ns >= self._wall_clock_contention_min_cpu_ns:
                self._step_history.append((wall_step_ns, max_cpu_step_ns))
            contention = self._contention_factor_locked()

            for snake_id, tracker in self._trackers.items():
                if tracker.killed:
                    continue
                current = tracker.last_cpu_ns  # already updated above
                delta_ns = cpu_deltas.get(snake_id, 0)
                step_times[snake_id] = delta_ns / 1e6
                self._exec_times_ms.setdefault(snake_id, []).append(delta_ns / 1e6)
                tracker.step_budget_ns = self._per_step_budget_ns
                tracker.step_start_wall_ns = now_wall
                tracker.step_start_cpu_ns = current

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

                if self._wall_accumulating_enabled and wall_step_ns > 0:
                    # excess_wall = wall_step - cpu_step * contention. A
                    # sleeping agent (cpu ~= 0) has excess ≈ wall_step; an
                    # agent legitimately slowed by contention has cpu *
                    # contention ≈ wall_step, so excess ≈ 0.
                    expected_wall_ns = int(delta_ns * contention)
                    excess_ns = max(0, wall_step_ns - expected_wall_ns)
                    tracker.wall_bank_remaining_ns += self._wall_accumulating_step_ns - excess_ns
                    if tracker.wall_bank_remaining_ns > self._wall_accumulating_max_ns:
                        tracker.wall_bank_remaining_ns = self._wall_accumulating_max_ns
                    if tracker.wall_bank_remaining_ns < 0:
                        log.warning(
                            "snake %s (%s) over sustained wall budget "
                            "(bank %.3fs, excess %.3fs this step, contention %.2f) -> killing",
                            snake_id, tracker.name,
                            tracker.wall_bank_remaining_ns / 1e9,
                            excess_ns / 1e9,
                            contention,
                        )
                        tracker.killed = True
                        tracker.kill_reason = "sustained_wall"
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
        "startup_cpu", "per_step", "sustained", "wall_clock", "sustained_wall",
        "init_failure", "dead", or None if the agent ran to clean completion
        / is still alive."""
        with self._lock:
            tracker = self._trackers.get(seat)
            return tracker.kill_reason if tracker else None

    def get_budgets(self) -> dict[str, float]:
        """Return the budget config as seconds, suitable for bundle metadata.

        Note: the per-step wall budget is computed adaptively each iteration
        of the poll loop and isn't a constant — what's recorded here are the
        knobs that drive that computation.
        """
        return {
            "per_step_seconds": self._per_step_budget_ns / 1e9,
            "initial_seconds": self._initial_extra_ns / 1e9,
            "startup_seconds": self._startup_budget_ns / 1e9,
            "accumulating_step_seconds": self._accumulating_step_ns / 1e9,
            "accumulating_initial_seconds": self._accumulating_initial_ns / 1e9,
            "accumulating_max_seconds": self._accumulating_max_ns / 1e9,
            "wall_clock_safety_factor": self._wall_clock_safety_factor,
            "wall_clock_hard_floor_seconds": self._wall_clock_hard_floor_ns / 1e9,
            "wall_clock_sleep_threshold_seconds": self._wall_clock_sleep_threshold_ns / 1e9,
            "wall_clock_psi_scale_k": self._wall_clock_psi_scale_k,
            "wall_clock_fallback_contention": self._wall_clock_fallback_contention,
            "wall_clock_max_contention": self._wall_clock_max_contention,
            "wall_clock_contention_min_cpu_seconds": self._wall_clock_contention_min_cpu_ns / 1e9,
            "wall_clock_min_history": float(self._wall_clock_min_history),
            "wall_accumulating_step_seconds": self._wall_accumulating_step_ns / 1e9,
            "wall_accumulating_initial_seconds": self._wall_accumulating_initial_ns / 1e9,
            "wall_accumulating_max_seconds": self._wall_accumulating_max_ns / 1e9,
        }

    # ── Contention measurement ───────────────────────────────────────────────
    #
    # Three tiers, evaluated in order. The first one that produces a usable
    # number wins. Floor at 1.0 so we never tell the budget "wall is less
    # than CPU" (would make the per-step budget shrink below CPU budget).

    def _contention_factor_locked(self) -> float:
        """Caller must hold self._lock. Result is clamped to
        [1.0, wall_clock_max_contention]."""
        cap = self._wall_clock_max_contention
        # Tier 1: measured from completed steps.
        if len(self._step_history) >= self._wall_clock_min_history:
            ratios = [w / c for (w, c) in self._step_history if c > 0]
            if ratios:
                return min(cap, max(1.0, sum(ratios) / len(ratios)))
        # Tier 2: PSI from the kernel.
        psi = self._read_psi_some_avg10()
        if psi is not None:
            # `psi` is a percentage in [0, 100]. Translate to a contention
            # multiplier: 0 → 1×, 50% → 1 + 0.5 * k, etc.
            return min(cap, max(1.0, 1.0 + (psi / 100.0) * self._wall_clock_psi_scale_k))
        else:
            log.warning("contention factor: no step history and failed to read PSI, using fallback contention %.1f",
                        self._wall_clock_fallback_contention)
        # Tier 3: configured fallback.
        return min(cap, max(1.0, self._wall_clock_fallback_contention))

    def _read_psi_some_avg10(self) -> float | None:
        path = self._wall_clock_psi_path
        if path is None or not path.exists():
            return None
        try:
            text = path.read_text()
        except OSError:
            return None
        # Line shape: "some avg10=12.34 avg60=... avg300=... total=..."
        for line in text.splitlines():
            if not line.startswith("some "):
                continue
            for token in line.split():
                if token.startswith("avg10="):
                    try:
                        return float(token[len("avg10="):])
                    except ValueError:
                        return None
        return None

    def _per_step_wall_budget_ns_locked(self) -> int:
        """The current per-step wall-clock budget. Caller must hold the lock."""
        contention = self._contention_factor_locked()
        budget = int(
            self._per_step_budget_ns * contention * self._wall_clock_safety_factor
        )
        return max(self._wall_clock_hard_floor_ns, budget)

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
                # Compute the per-step wall budget once per iteration — it's
                # the same for every tracker, and we don't want to read PSI N
                # times per cycle.
                wall_budget_ns = (
                    self._per_step_wall_budget_ns_locked()
                    if self._wall_clock_enabled else 0
                )
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
                        continue

                    # Wall-clock guard: catches agents that block on sleep() /
                    # I/O instead of computing. We require *both* (a) wall-clock
                    # since the last step started has blown the contention-
                    # adaptive budget and (b) CPU usage in that window is
                    # essentially zero — that rules out an agent that's
                    # legitimately burning its full CPU budget under load.
                    if (
                        self._wall_clock_enabled
                        and not tracker.in_startup
                        and tracker.step_start_wall_ns
                    ):
                        wall_elapsed = time.monotonic_ns() - tracker.step_start_wall_ns
                        cpu_in_window = current - tracker.step_start_cpu_ns
                        if (
                            wall_elapsed > wall_budget_ns
                            and cpu_in_window <= self._wall_clock_sleep_threshold_ns
                        ):
                            log.warning(
                                "snake %s (%s) wall-clock %.3fs > budget %.3fs but "
                                "cpu only %.3fs -> killing (sleeping/hung)",
                                snake_id, tracker.name,
                                wall_elapsed / 1e9, wall_budget_ns / 1e9,
                                cpu_in_window / 1e9,
                            )
                            tracker.killed = True
                            tracker.kill_reason = "wall_clock"
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
