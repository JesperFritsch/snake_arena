# services/runner/runner/agent_container_manager.py
import logging
import threading
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Callable

from docker.errors import APIError, NotFound
from docker.models.containers import Container

from snake_sim.environment.interfaces.loop_observer_interface import ILoopObserver
from snake_sim.environment.types import (
    LoopDecisionData,
    LoopStartData,
    LoopStepData,
    LoopStopData,
)

log = logging.getLogger(__name__)

_CGROUP_BASE = Path("/sys/fs/cgroup")

# A seat is "busy" this window if its cpu/wall ratio is at least this.
# The CPU kills (per-step and sustained) only fire when busy_ratio is
# above the threshold — otherwise the cgroup window was wide and the
# agent was idle in most of it (gRPC keepalive, GC, idle threads). That
# kind of CPU is the wall bank's territory, not the CPU bank's. 0.5 =
# "the agent was on-CPU for at least half the window."
_BUSY_RATIO = 0.5

# (No standalone CPU floor for the per-step wall kill — the busy_ratio
# threshold is the right signal. A long wall with low busy_ratio means
# the container was mostly idle, regardless of how many absolute ns of
# background ticks accumulated.)

# Wall-to-CPU multiplier for the sustained-wall bank. The bank debits
# `max(0, response_wall - cpu * WALL_K)`; a value of 3 allows up to 3×
# the agent's CPU time as wall before any excess is debited. Fixed —
# no adaptive contention factor. On dedicated runner hosts the real
# contention is close to 1× anyway, and 3× absorbs normal sim
# overhead + gRPC dispatch + scheduling jitter. If host pressure
# becomes a real production issue, add adaptive contention back here
# with data.
_WALL_K = 3.0


@dataclass
class _AgentTracker:
    container: Container
    name: str
    # Cumulative cgroup cpu_ns at the end of the previous notify_step
    # (or at set_agent_containers, before the match started).
    last_cpu_ns: int = 0
    # Wall + CPU snapshot at the start of the *current* step. The poll
    # loop subtracts these to compute cpu_in_step / wall_in_step.
    step_start_wall_ns: int = 0
    step_start_cpu_ns: int = 0
    # Sustained-CPU bank in ns. Refilled at notify_step (when busy),
    # debited by total cgroup cpu_delta. < 0 → kill.
    cpu_bank_ns: int = 0
    # Sustained-wall bank in ns. Refilled at notify_step, debited by
    # excess wall (response_wall - cpu * WALL_K). < 0 → kill.
    wall_bank_ns: int = 0
    # Set when notify_decision fires this step. Poll loop skips agents
    # that have already responded — they aren't holding up the sim.
    responded_this_step: bool = False
    # Sim's per-decision wall duration in ns for the current step
    # (LoopDecisionData.wall_time_ns). 0 if not received yet.
    response_wall_ns: int = 0
    # Most recent sim step index seen for this seat (set in notify_step).
    # Sanity-checked against LoopDecisionData.step_idx for drift detection.
    last_step_seen: int = -1
    # True from set_agent_containers until notify_start. The poll loop
    # skips startup-phase trackers — the gRPC init wall-clock timeout
    # is the bound on this phase, not the poll loop.
    in_startup: bool = True
    killed: bool = False
    # One of: "startup_cpu", "per_step", "sustained", "wall_clock",
    # "sustained_wall", "init_failure", "dead", "post_dev_cleanup". None
    # for alive seats and clean shutdowns.
    kill_reason: str | None = None


class AgentContainerManager(ILoopObserver):
    """
    Enforces CPU and wall-clock budgets for agent containers.

    The five rules, all together:

    1. Per-step CPU cap (poll loop, ~10 ms cadence): if
       `cpu_in_step > per_step_cpu_budget` AND `busy_ratio >= 0.5`, kill
       with reason "per_step". The busy_ratio guard prevents false kills
       when the cgroup window stretched (other containers being SIGKILLed
       mid-step, slow notify_step delivery) and the CPU delta is dominated
       by idle background work the agent didn't initiate.

    2. Per-step wall guard (poll loop): if `wall_in_step >
       per_step_wall_budget` AND `cpu_in_step < ~1 ms`, kill with reason
       "wall_clock". This catches sleepers and hangs — agents that hold
       up the sim without computing.

    3. Sustained CPU bank (notify_step): refilled by
       `sustained_cpu_refill_seconds` per step, debited by the full
       cgroup CPU delta for the cycle. < 0 → kill "sustained". Refill +
       debit only fires when `busy_ratio >= 0.5` for the cycle —
       background CPU on idle cycles isn't charged.

    4. Sustained wall bank (notify_step): refilled by
       `sustained_wall_refill_seconds` per step, debited by
       `max(0, response_wall - cpu * WALL_K)`. `response_wall` is the
       sim's per-decision wall (LoopDecisionData.wall_time_ns) — per
       snake, not the global cycle, so a fast snake stuck behind a slow
       neighbour doesn't pay for the neighbour. < 0 → kill "sustained_wall".

    5. Snake-died check (notify_step): if `alive_states[snake_id]` is
       False, kill the container with reason "dead". Normal in-game
       outcome, not a budget violation.

    Lifecycle:
    - `set_agent_containers` records a baseline cpu_ns per container and
      starts the poll thread. The poll loop sees `in_startup=True` and
      does nothing until `notify_start` flips the flag.
    - `notify_start` runs a one-shot startup CPU check (current cpu_ns
      minus the baseline must fit `startup_cpu_budget`); over-budget
      seats are killed with reason "startup_cpu". Seats the sim dropped
      during gRPC init (absent from `snake_tags`) get reason
      "init_failure". Surviving seats have their banks initialised and
      `in_startup` cleared.
    - Test matches: when the dev seat is killed,
      `kill_opponents_after_dev_dies_steps` additional sim steps later
      all surviving opponents are killed with reason "post_dev_cleanup"
      so the replay has a tail to show what happened.

    No PSI parsing, no adaptive contention, no mid-step projected bank
    kills, no continuous startup-phase CPU monitoring. Rule 1 + rule 2
    in the poll loop, rules 3-5 at notify_step. ~250 lines.
    """

    def __init__(
        self,
        per_step_cpu_budget_seconds: float = 0.05,
        per_step_wall_budget_seconds: float = 1.0,
        startup_cpu_budget_seconds: float = 0.2,
        sustained_cpu_refill_seconds: float = 0.01,
        sustained_cpu_initial_seconds: float = 0.05,
        sustained_cpu_max_seconds: float = 0.5,
        sustained_wall_refill_seconds: float = 0.05,
        sustained_wall_initial_seconds: float = 0.5,
        sustained_wall_max_seconds: float = 1.0,
        poll_interval_s: float = 0.01,
        # Per-step callback: (sim_step, {seat: cpu_ms_this_step}). Keyed
        # by seat so live consumers (redis observer) don't have to know
        # the sim's snake_id assignments.
        on_exec_times: Callable[[int, dict[int, float]], None] | None = None,
        # For test matches: once the dev agent dies, end the match for
        # the opponents after this many *additional* sim steps so the
        # replay still shows a few frames of context. None disables.
        # Requires `dev_seat` to be set.
        kill_opponents_after_dev_dies_steps: int | None = None,
        # Seat index of the dev agent for test matches; None for ranked.
        dev_seat: int | None = None,
    ):
        super().__init__()
        self._per_step_cpu_budget_ns = int(per_step_cpu_budget_seconds * 1e9)
        self._per_step_wall_budget_ns = int(per_step_wall_budget_seconds * 1e9)
        self._startup_cpu_budget_ns = int(startup_cpu_budget_seconds * 1e9)
        self._sustained_cpu_refill_ns = int(sustained_cpu_refill_seconds * 1e9)
        self._sustained_cpu_initial_ns = int(sustained_cpu_initial_seconds * 1e9)
        self._sustained_cpu_max_ns = int(sustained_cpu_max_seconds * 1e9)
        self._sustained_wall_refill_ns = int(sustained_wall_refill_seconds * 1e9)
        self._sustained_wall_initial_ns = int(sustained_wall_initial_seconds * 1e9)
        self._sustained_wall_max_ns = int(sustained_wall_max_seconds * 1e9)
        self._poll_interval_s = poll_interval_s
        self._on_exec_times = on_exec_times
        self._kill_opp_after_dev_dies_steps = kill_opponents_after_dev_dies_steps
        if kill_opponents_after_dev_dies_steps is not None and dev_seat is None:
            raise ValueError(
                "kill_opponents_after_dev_dies_steps requires dev_seat to be set"
            )
        self._dev_seat = dev_seat
        self._dev_died_at_step: int | None = None

        # All match-lifecycle state is keyed by SEAT (the runner's index,
        # stable across the match). The sim assigns its own snake_id
        # values which we learn from notify_start's snake_tags map — they
        # are NOT guaranteed to match seat indices. We join the two via
        # the *tag* (the target string passed to the sim as --ext-targets).
        self._trackers: dict[int, _AgentTracker] = {}
        # seat → list of per-step CPU times in ms; each seat's list ends
        # at the step where it died.
        self._exec_times_ms: dict[int, list[float]] = {}
        # seat → list of per-step sim cycle walls in ms; same shape as
        # exec_times. Globally the same per step (sim's total_time) but
        # per-seat so each list ends with the seat.
        self._wall_step_times_ms: dict[int, list[float]] = {}
        # Runner-controlled mappings, installed by set_agent_containers.
        self._seat_to_container: dict[int, Container] = {}
        self._target_by_seat: dict[int, str] = {}
        self._seat_by_target: dict[str, int] = {}
        # Sim-controlled mappings, installed by notify_start.
        self._snake_id_by_seat: dict[int, int] = {}
        self._seat_by_snake_id: dict[int, int] = {}

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def set_agent_containers(
        self,
        seat_to_container: dict[int, Container],
        target_by_seat: dict[int, str],
    ) -> None:
        """Record the containers and start the poll thread. Baseline cpu_ns
        is captured per container — the startup CPU check at notify_start
        measures usage since this point."""
        if set(seat_to_container) != set(target_by_seat):
            raise ValueError(
                "seat_to_container and target_by_seat must cover the same seats"
            )
        self._seat_to_container = seat_to_container
        self._target_by_seat = dict(target_by_seat)
        self._seat_by_target = {t: s for s, t in target_by_seat.items()}
        self._snake_id_by_seat.clear()
        self._seat_by_snake_id.clear()

        # Stop any prior poll thread cleanly before rebuilding trackers.
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
                    name=target_by_seat[seat],
                    last_cpu_ns=max(baseline, 0),
                )
                log.info(
                    "startup monitoring: seat %d tag=%s (cpu budget %.1fs)",
                    seat, target_by_seat[seat],
                    self._startup_cpu_budget_ns / 1e9,
                )

        self._thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="cpu-budget"
        )
        self._thread.start()

    def notify_start(self, data: LoopStartData) -> None:
        """Run the one-shot startup CPU check, init banks, and flip
        surviving seats out of startup mode so the poll loop's per-step
        checks begin."""
        snake_tags = data.env_meta_data.snake_tags
        now_wall = time.monotonic_ns()
        with self._lock:
            self._wall_step_times_ms.clear()
            self._exec_times_ms.clear()
            self._snake_id_by_seat.clear()
            self._seat_by_snake_id.clear()

            active_seats: set[int] = set()
            for snake_id, tag in snake_tags.items():
                seat = self._seat_by_target.get(tag)
                if seat is None:
                    log.warning(
                        "notify_start: tag %r (snake_id=%d) has no matching seat",
                        tag, snake_id,
                    )
                    continue
                tracker = self._trackers.get(seat)
                if tracker is None:
                    log.warning(
                        "notify_start: no tracker for seat %d (tag=%r)", seat, tag
                    )
                    continue

                # One-shot startup CPU check: CPU burned between
                # set_agent_containers (baseline in last_cpu_ns) and now
                # must fit the startup budget.
                current = self._read_cpu_ns(tracker.container)
                if current >= 0:
                    startup_used = max(0, current - tracker.last_cpu_ns)
                    if startup_used > self._startup_cpu_budget_ns:
                        log.warning(
                            "seat %d (tag=%s) startup cpu %.3fs > budget %.3fs"
                            " -> killing",
                            seat, tag, startup_used / 1e9,
                            self._startup_cpu_budget_ns / 1e9,
                        )
                        tracker.killed = True
                        tracker.kill_reason = "startup_cpu"
                        self._kill_seat(seat)
                        continue
                    tracker.last_cpu_ns = current

                self._snake_id_by_seat[seat] = snake_id
                self._seat_by_snake_id[snake_id] = seat
                active_seats.add(seat)
                tracker.name = tag
                tracker.step_start_wall_ns = now_wall
                tracker.step_start_cpu_ns = tracker.last_cpu_ns
                tracker.cpu_bank_ns = self._sustained_cpu_initial_ns
                tracker.wall_bank_ns = self._sustained_wall_initial_ns
                tracker.responded_this_step = False
                tracker.response_wall_ns = 0
                tracker.last_step_seen = -1
                tracker.in_startup = False
                log.info(
                    "per-step monitoring: seat %d (snake_id=%d, tag=%s)",
                    seat, snake_id, tag,
                )

            # Seats with a tracker but no snake_id in snake_tags were
            # dropped during sim init (gRPC timeout, agent crash before
            # SetInitData). Kill them so the runner can surface the
            # init-failure outcome upstream.
            for seat, tracker in self._trackers.items():
                if seat not in active_seats and not tracker.killed:
                    log.info(
                        "seat %d (tag=%s) absent from match start"
                        " (init failure) — killing container",
                        seat, self._target_by_seat.get(seat, "?"),
                    )
                    tracker.killed = True
                    tracker.kill_reason = "init_failure"
                    self._kill_seat(seat)

        # Ensure the poll thread is running (set_agent_containers
        # already started it; defensive for the manager being re-used
        # across matches).
        if self._thread is None or not self._thread.is_alive():
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._poll_loop, daemon=True, name="cpu-budget"
            )
            self._thread.start()

    def notify_decision(self, data: LoopDecisionData) -> None:
        """Record the sim's per-snake decision wall (lag-immune — measured
        inside the sim's executor, not from receive time) and clear the
        seat from the poll loop's kill candidates for the rest of this
        cycle."""
        with self._lock:
            seat = self._seat_by_snake_id.get(data.snake_id)
            if seat is None:
                return
            tracker = self._trackers.get(seat)
            if tracker is None or tracker.killed:
                return
            expected = tracker.last_step_seen + 1
            if data.step_idx != expected:
                log.warning(
                    "seat %d (tag=%s) notify_decision step_idx mismatch:"
                    " got %d, expected %d (last_step_seen=%d)",
                    seat, tracker.name,
                    data.step_idx, expected, tracker.last_step_seen,
                )
            tracker.responded_this_step = True
            tracker.response_wall_ns = data.wall_time_ns

    def notify_step(self, data: LoopStepData) -> None:
        """Per-step bank updates and snake-death cleanup. Each seat's
        cycle window closes at this call; the per-step poll-loop counters
        reset for the next cycle."""
        # Sim's own cycle wall — lag-immune, not derived from manager-side
        # receive-time deltas (those would be inflated by TCP/queue lag).
        wall_step_ns = int(data.total_time * 1e9) if data.total_time > 0 else 0
        wall_step_ms = wall_step_ns / 1e6
        step_times: dict[int, float] = {}
        now_wall = time.monotonic_ns()

        with self._lock:
            for seat, tracker in self._trackers.items():
                if tracker.killed:
                    continue

                current = self._read_cpu_ns(tracker.container)
                if current < 0:
                    # cgroup gone — container exited. Hold last reading
                    # so cpu_delta is 0; the alive_states check below
                    # will tag the seat dead.
                    current = tracker.last_cpu_ns
                cpu_delta = max(0, current - tracker.last_cpu_ns)
                tracker.last_cpu_ns = current

                step_times[seat] = cpu_delta / 1e6
                self._exec_times_ms.setdefault(seat, []).append(cpu_delta / 1e6)
                self._wall_step_times_ms.setdefault(seat, []).append(wall_step_ms)

                # Cap cpu_delta at sim_wall_step for budget accounting.
                # cpu_delta is cgroup CPU between consecutive *manager-side*
                # notify_step processings; under normal flow that window
                # matches sim_wall_step closely, but when manager processing
                # gets delayed (poll-loop lock contention, host pressure)
                # multiple sim steps' worth of CPU accumulate in cgroup
                # before the first notify_step in the burst gets processed.
                # Without the cap, that first event debits prior steps' CPU
                # against this step's refill and false-kills the agent.
                # With nano_cpus=1.0 the agent physically cannot have used
                # more CPU than wall during a single sim step, so
                # sim_wall_step_ns is the natural ceiling. cpu_delta itself
                # is kept uncharged (we set last_cpu_ns = current above) —
                # the leaked accounting represents work in prior steps the
                # manager couldn't attribute per-step; nano_cpus is the
                # absolute backstop. exec_times still records the raw
                # delta for diagnostics.
                cpu_delta_for_bank = (
                    min(cpu_delta, wall_step_ns) if wall_step_ns > 0 else 0
                )
                busy = (
                    (cpu_delta_for_bank / wall_step_ns)
                    if wall_step_ns > 0 else 0.0
                )

                # Sustained CPU bank. Skip when the cycle was idle for
                # this seat — busy_ratio low means the wide cgroup
                # window was background noise, not 10ms-per-step abuse.
                # The wall bank catches the "low CPU, high wall" case.
                if busy >= _BUSY_RATIO:
                    tracker.cpu_bank_ns += (
                        self._sustained_cpu_refill_ns - cpu_delta_for_bank
                    )
                    if tracker.cpu_bank_ns > self._sustained_cpu_max_ns:
                        tracker.cpu_bank_ns = self._sustained_cpu_max_ns
                    if tracker.cpu_bank_ns < 0:
                        log.warning(
                            "seat %d (tag=%s) over sustained cpu budget"
                            " (bank %.3fs, used %.3fs, busy %.2f) -> killing",
                            seat, tracker.name,
                            tracker.cpu_bank_ns / 1e9,
                            cpu_delta_for_bank / 1e9, busy,
                        )
                        tracker.killed = True
                        tracker.kill_reason = "sustained"
                        self._kill_seat(seat)
                        continue

                # Sustained wall bank. response_wall is sim's per-snake
                # measurement when present; falls back to the global
                # cycle wall only if notify_decision never fired for
                # this seat this step (shouldn't happen for an alive
                # snake — the sim wouldn't have moved on).
                response_wall_ns = tracker.response_wall_ns or wall_step_ns
                expected_wall_ns = int(cpu_delta * _WALL_K)
                excess_ns = max(0, response_wall_ns - expected_wall_ns)
                tracker.wall_bank_ns += self._sustained_wall_refill_ns - excess_ns
                if tracker.wall_bank_ns > self._sustained_wall_max_ns:
                    tracker.wall_bank_ns = self._sustained_wall_max_ns
                if tracker.wall_bank_ns < 0:
                    log.warning(
                        "seat %d (tag=%s) over sustained wall budget"
                        " (bank %.3fs, excess %.3fs, response_wall %.3fs,"
                        " cpu %.3fs) -> killing",
                        seat, tracker.name,
                        tracker.wall_bank_ns / 1e9, excess_ns / 1e9,
                        response_wall_ns / 1e9, cpu_delta / 1e9,
                    )
                    tracker.killed = True
                    tracker.kill_reason = "sustained_wall"
                    self._kill_seat(seat)
                    continue

                # Reset for next cycle (step_start_* feed the poll loop's
                # cpu_in_step / wall_in_step subtractions).
                tracker.step_start_wall_ns = now_wall
                tracker.step_start_cpu_ns = current
                tracker.responded_this_step = False
                tracker.response_wall_ns = 0
                tracker.last_step_seen = data.step

                # alive_states is keyed by snake_id, not seat.
                snake_id = self._snake_id_by_seat.get(seat)
                if snake_id is None:
                    continue
                if not data.alive_states.get(snake_id, False):
                    log.info(
                        "seat %d (tag=%s, snake_id=%d) is dead — killing container",
                        seat, tracker.name, snake_id,
                    )
                    tracker.killed = True
                    tracker.kill_reason = "dead"
                    self._kill_seat(seat)

            # Test-match opponent cleanup.
            if self._kill_opp_after_dev_dies_steps is not None:
                dev = (
                    self._trackers.get(self._dev_seat)
                    if self._dev_seat is not None else None
                )
                if dev is not None and dev.killed:
                    if self._dev_died_at_step is None:
                        self._dev_died_at_step = data.step
                    elif (
                        data.step - self._dev_died_at_step
                        >= self._kill_opp_after_dev_dies_steps
                    ):
                        for seat, t in self._trackers.items():
                            if seat == self._dev_seat or t.killed:
                                continue
                            log.info(
                                "test match: dev died at step %d, "
                                "ending opponent seat %d at step %d",
                                self._dev_died_at_step, seat, data.step,
                            )
                            t.killed = True
                            t.kill_reason = "post_dev_cleanup"
                            self._kill_seat(seat)

        if step_times and self._on_exec_times is not None:
            try:
                self._on_exec_times(data.step, step_times)
            except Exception:
                log.warning("on_exec_times callback failed", exc_info=True)

    def notify_stop(self, data: LoopStopData) -> None:
        self._shutdown()

    # ── Public accessors used by runner/match.py ─────────────────────────────

    def get_exec_times(self) -> dict[int, list[float]]:
        """Per-seat per-step CPU times in ms."""
        with self._lock:
            return {k: list(v) for k, v in self._exec_times_ms.items()}

    def get_wall_step_times(self) -> dict[int, list[float]]:
        """Per-seat per-step sim cycle wall in ms. Sourced from the sim
        (LoopStepData.total_time); values are the same across seats at
        any given step. Shape mirrors exec_times so each seat's list
        ends with the seat."""
        with self._lock:
            return {k: list(v) for k, v in self._wall_step_times_ms.items()}

    def get_budgets(self) -> dict[str, float]:
        """Budget config as seconds, for inclusion in the match bundle."""
        return {
            "per_step_cpu_seconds": self._per_step_cpu_budget_ns / 1e9,
            "per_step_wall_seconds": self._per_step_wall_budget_ns / 1e9,
            "startup_cpu_seconds": self._startup_cpu_budget_ns / 1e9,
            "sustained_cpu_refill_seconds": self._sustained_cpu_refill_ns / 1e9,
            "sustained_cpu_initial_seconds": self._sustained_cpu_initial_ns / 1e9,
            "sustained_cpu_max_seconds": self._sustained_cpu_max_ns / 1e9,
            "sustained_wall_refill_seconds": self._sustained_wall_refill_ns / 1e9,
            "sustained_wall_initial_seconds": self._sustained_wall_initial_ns / 1e9,
            "sustained_wall_max_seconds": self._sustained_wall_max_ns / 1e9,
            "wall_to_cpu_k": _WALL_K,
            "busy_ratio_threshold": _BUSY_RATIO,
        }

    def get_init_failed_seats(self) -> list[int]:
        """Seats that were killed with reason 'init_failure'."""
        with self._lock:
            return [
                seat for seat, t in self._trackers.items()
                if t.kill_reason == "init_failure"
            ]

    def get_kill_reason(self, seat: int) -> str | None:
        with self._lock:
            tracker = self._trackers.get(seat)
            return tracker.kill_reason if tracker else None

    def get_seat_by_snake_id(self) -> dict[int, int]:
        with self._lock:
            return dict(self._seat_by_snake_id)

    def reset(self) -> None:
        self._shutdown()
        with self._lock:
            self._trackers.clear()
            self._seat_to_container.clear()

    def close(self) -> None:
        self._shutdown()

    # ── Internals ────────────────────────────────────────────────────────────

    def _shutdown(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def _poll_loop(self) -> None:
        """The only async kill path — runs at ~poll_interval_s cadence.
        Two checks per tick per tracker, plus the gates. That's it."""
        log.info("cpu budget poll loop started")
        while not self._stop_event.is_set():
            with self._lock:
                for seat, tracker in self._trackers.items():
                    if tracker.killed or tracker.in_startup:
                        continue
                    if tracker.responded_this_step:
                        # Agent already answered this cycle — it's not
                        # holding up the sim and any further CPU it
                        # burns will be debited at the next notify_step.
                        continue
                    current = self._read_cpu_ns(tracker.container)
                    if current < 0:
                        continue
                    cpu_in_step = current - tracker.step_start_cpu_ns
                    wall_in_step = (
                        time.monotonic_ns() - tracker.step_start_wall_ns
                        if tracker.step_start_wall_ns else 0
                    )
                    busy = (
                        cpu_in_step / wall_in_step if wall_in_step > 0 else 0.0
                    )

                    # Rule 1: per-step CPU cap. busy_ratio guard
                    # avoids false-killing on stretched cgroup windows.
                    if (
                        cpu_in_step > self._per_step_cpu_budget_ns
                        and busy >= _BUSY_RATIO
                    ):
                        log.warning(
                            "seat %d (tag=%s) per-step cpu %.3fs > budget"
                            " %.3fs (busy %.2f) -> killing",
                            seat, tracker.name,
                            cpu_in_step / 1e9,
                            self._per_step_cpu_budget_ns / 1e9, busy,
                        )
                        tracker.killed = True
                        tracker.kill_reason = "per_step"
                        self._kill_seat(seat)
                        continue

                    # Rule 2: stalled — long wall and the agent isn't
                    # actively using CPU. The busy_ratio threshold is
                    # symmetric with rule 1 (which kills only when the
                    # agent IS busy); here we kill only when the agent
                    # IS NOT busy. An agent doing real CPU work has
                    # busy ≈ 1.0 even when slow; a sleeper's busy stays
                    # near 0 because the container's background CPU is
                    # tiny compared to wall.
                    if (
                        wall_in_step > self._per_step_wall_budget_ns
                        and busy < _BUSY_RATIO
                    ):
                        log.warning(
                            "seat %d (tag=%s) per-step wall %.3fs > budget"
                            " %.3fs, busy %.2f (cpu %.3fs) -> killing (sleeping/hung)",
                            seat, tracker.name,
                            wall_in_step / 1e9,
                            self._per_step_wall_budget_ns / 1e9,
                            busy, cpu_in_step / 1e9,
                        )
                        tracker.killed = True
                        tracker.kill_reason = "wall_clock"
                        self._kill_seat(seat)
            self._stop_event.wait(self._poll_interval_s)
        log.info("cpu budget poll loop stopped")

    @staticmethod
    def _read_cpu_ns(container: Container) -> int:
        """Read cumulative cgroup v2 cpu.stat usage_usec, return ns.
        Returns -1 if the container/cgroup is gone."""
        container_id = container.id
        if container_id is None:
            raise ValueError("container has no ID")

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
                raise ValueError("failed to read cpu.stat")
            for line in content.splitlines():
                if line.startswith("usage_usec"):
                    try:
                        return int(line.split()[1]) * 1000
                    except (ValueError, IndexError):
                        raise ValueError("failed to parse cpu.stat")
            raise ValueError("failed to find usage_usec in cpu.stat")
        raise ValueError("container has no ID")

    def _kill_seat(self, seat_id: int) -> None:
        """SIGKILL the container — uncatchable, so the agent goes down
        regardless of its signal handlers. The kernel closes its TCP
        sockets on exit, the sim's gRPC stream errors, and the sim
        proceeds. Async at the docker layer (~10–50 ms); the agent may
        answer 1–2 more in-flight steps before its process dies."""
        tracker = self._trackers.get(seat_id)
        if tracker is None:
            return
        try:
            tracker.container.kill()
        except NotFound:
            pass  # already gone — goal state met
        except APIError as e:
            # 409 = "container is not running"; anything else is unexpected.
            if e.response.status_code == 409:
                log.debug("container %s already stopped", tracker.container.name)
            else:
                log.warning(
                    "failed to kill container %s: %s",
                    tracker.container.name, e,
                )
