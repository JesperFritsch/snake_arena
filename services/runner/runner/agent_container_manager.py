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

# When the poll loop sees `used_this_step > step_budget_ns`, the per-step
# kill only fires if the snake was *busy* for most of the window — i.e.
# cpu/wall >= this ratio. A snake that's idle for most of the budget window
# is only "over" because notify_step has been delayed (slow observer chain,
# socket buffering) and CPU has accumulated across multiple intended sim
# steps; in that case the accumulating CPU budget handles enforcement on
# the next notify_step. 0.5 = "at least 50% of the window was CPU work."
_PER_STEP_KILL_BUSY_RATIO = 0.5


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
    # `wall_accumulating_step_ns` and debited by *excess* wall (the snake's
    # own response wall minus the part explained by cpu_step under current
    # contention). Bounds how much "sleeping just under the per-step wall
    # threshold" an agent can do across the match.
    wall_bank_remaining_ns: int = 0
    # Wall-clock and CPU snapshot at the start of the current step. The poll
    # loop uses these to catch agents that are sleeping / hanging — they
    # blow the per-step wall-clock budget without ever burning CPU.
    step_start_wall_ns: int = 0
    step_start_cpu_ns: int = 0
    # Set when the sim notifies us that this snake has returned its decision
    # for the current step. Until that happens, the snake is the (or a)
    # bottleneck holding the sim from advancing — only pending snakes are
    # kill candidates for the wall-clock guards. Reset at every notify_step
    # for surviving snakes.
    responded_this_step: bool = False
    # Wall time (monotonic_ns) when this snake's decision arrived this step,
    # i.e. when notify_decision fired for it. 0 = not received yet. Used to
    # compute the snake's individual response wall for the sustained-wall
    # excess debit at notify_step.
    response_wall_ns: int = 0
    # True from set_agent_containers until notify_start claims this seat.
    # Used so the poll loop can distinguish a startup-phase CPU kill from a
    # per-step CPU kill — the budget is `startup_budget_ns` in either case
    # but the human-readable reason is different.
    in_startup: bool = True
    killed: bool = False
    # One of: None (alive / clean exit), "startup_cpu", "per_step",
    # "sustained", "wall_clock", "sustained_wall", "init_failure", "dead",
    # "post_dev_cleanup". Used so the runner can explain a kill to the
    # dev agent's console.
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
    `wall_clock_safety_factor` > 0). Both rely on the sim firing
    `notify_decision(snake_id)` as soon as a snake's response for the
    current step arrives. A snake that has *not* yet fired notify_decision
    is "pending" — it's the (or a) bottleneck holding the sim from
    advancing. Only pending snakes are kill candidates here; snakes that
    already responded this step can't be the cause of a slow wall, even if
    wall_step looks bad globally because a neighbour is hanging.

    Per-step wall budget — if a pending snake's wall_elapsed exceeds the
    contention-adjusted budget, it's killed. Reason "wall_clock" if its
    cpu_in_window is under the step CPU budget (sleeping/blocked) or
    "per_step" if it's over (single slow step holding the sim). The budget
    is computed adaptively each poll iteration:
      1. Rolling mean of (wall_step / max_cpu_step) over the last K
         completed steps, when there are at least `wall_clock_min_history`
         steps of history.
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
    wall*, defined as `max(0, snake_response_wall - cpu_step * contention)`.
    `snake_response_wall` is the time between the step's start and when the
    sim acked this snake's decision (per-snake, not the global wall_step) —
    that's what isolates each snake from its neighbours. A sleeping agent
    contributes ~0 CPU but a large response_wall, so essentially all of it
    is "excess" and drains the bank fast. An agent legitimately slowed by
    contention sees expected_wall ≈ response_wall, so excess is ~0. Bank is
    capped at `wall_accumulating_max_seconds`; when it goes negative the
    agent is killed.

    Containers are also killed as soon as their snake is marked dead by the
    sim (alive_states false in notify_step, or absent from notify_start
    because it was dropped during init).
    """

    def __init__(
        self,
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
        # Wall-clock accumulating guard. Strict by default — caps long-run
        # "sleep just under the line" abuse without affecting honest agents.
        wall_accumulating_step_seconds: float = 0.0,
        wall_accumulating_initial_seconds: float = 0.5,
        wall_accumulating_max_seconds: float = 1.0,
        poll_interval_s: float = 0.01,
        # Per-step callback: (sim_step, {seat: cpu_ms_this_step}). Keyed by
        # seat so live consumers (redis observer) don't have to know the
        # sim's snake_id assignments.
        on_exec_times: Callable[[int, dict[int, float]], None] | None = None,
        # For test matches: once the dev agent dies, end the match for
        # the opponents after this many *additional* sim steps so the
        # replay still shows a few frames of context after the death.
        # None disables (use for ranked matches where the rest of the
        # bracket should keep playing). Requires `dev_seat` to be set.
        kill_opponents_after_dev_dies_steps: int | None = None,
        # Seat index of the dev agent for test matches; None for ranked.
        # Drives kill_opponents_after_dev_dies_steps and tells the runner
        # which seat's exec_times to anchor the kill banner against.
        dev_seat: int | None = None,
    ):
        super().__init__()
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
        self._wall_clock_psi_path = Path(wall_clock_psi_path) if wall_clock_psi_path else None
        self._wall_clock_psi_scale_k = wall_clock_psi_scale_k
        self._wall_clock_fallback_contention = wall_clock_fallback_contention
        self._wall_clock_min_history = wall_clock_min_history
        self._wall_clock_max_contention = wall_clock_max_contention
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
        self._kill_opp_after_dev_dies_steps = kill_opponents_after_dev_dies_steps
        if kill_opponents_after_dev_dies_steps is not None and dev_seat is None:
            raise ValueError(
                "kill_opponents_after_dev_dies_steps requires dev_seat to be set"
            )
        self._dev_seat = dev_seat
        # First sim step number at which the dev seat was observed dead.
        # Set once and used as the reference point for the opponent-cleanup
        # deadline above.
        self._dev_died_at_step: int | None = None

        # All match-lifecycle state is keyed by SEAT (the runner's index,
        # stable across the match). The sim assigns its own snake_id values
        # which we can only learn from notify_start's snake_tags map — and
        # they are NOT guaranteed to match seat indices. We join the two
        # via the *tag* (the target string we passed to the sim).
        self._trackers: dict[int, _AgentTracker] = {}
        # Per-step CPU time in ms, per seat. Each seat's list ends at the
        # step where it died — step i for seat s aligns with that seat's
        # wall_step_times_ms[s][i].
        self._exec_times_ms: dict[int, list[float]] = {}
        # Wall time between consecutive notify_step events, per seat. The
        # value is globally the same at any given step (the sim's cycle
        # time), but storing per-seat means each seat's list ends when it
        # dies — same shape as exec_times_ms.
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
        """Start startup-phase monitoring as soon as containers are ready.

        `target_by_seat` tells the manager which sim-side tag each seat
        will appear as in notify_start's snake_tags — i.e. the target
        string ("agent_3:50051") passed to the sim as --ext-targets. The
        sim's snake_id assignments are arbitrary and only join back to
        the runner's seats via this tag. Keys of both maps must match.
        """
        if set(seat_to_container) != set(target_by_seat):
            raise ValueError(
                "seat_to_container and target_by_seat must cover the same seats"
            )
        self._seat_to_container = seat_to_container
        self._target_by_seat = dict(target_by_seat)
        self._seat_by_target = {t: s for s, t in target_by_seat.items()}
        self._snake_id_by_seat.clear()
        self._seat_by_snake_id.clear()

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
                    name=target_by_seat[seat],  # tag is informative until notify_start refines anything
                    last_cpu_ns=max(baseline, 0),
                    step_budget_ns=self._startup_budget_ns,
                )
                log.info(
                    "cpu startup monitoring: seat %d tag=%s (budget %.1fs)",
                    seat, target_by_seat[seat], self._startup_budget_ns / 1e9,
                )

        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="cpu-budget")
        self._thread.start()

    def notify_start(self, data: LoopStartData):
        # snake_tags is the sim's snake_id -> tag mapping, only live snakes.
        # The tag equals the target string we passed in via --ext-targets,
        # so we can join it back to our seat layout via _seat_by_target.
        snake_tags = data.env_meta_data.snake_tags
        now_wall = time.monotonic_ns()
        with self._lock:
            # Seed the step-history wall reference and clear stale samples
            # from a previous match.
            self._last_notify_step_wall_ns = now_wall
            self._step_history.clear()
            # Reset per-match diagnostics so a daemon that runs multiple
            # matches in one process doesn't carry wall-times across matches.
            self._wall_step_times_ms.clear()
            self._exec_times_ms.clear()
            self._snake_id_by_seat.clear()
            self._seat_by_snake_id.clear()

            active_seats: set[int] = set()
            for snake_id, tag in snake_tags.items():
                seat = self._seat_by_target.get(tag)
                if seat is None:
                    log.warning(
                        "notify_start: tag %r (snake_id=%d) has no matching seat; "
                        "runner's target_by_seat is out of sync with the sim",
                        tag, snake_id,
                    )
                    continue
                tracker = self._trackers.get(seat)
                if tracker is None:
                    log.warning(
                        "notify_start: no tracker for seat %d (tag=%r, snake_id=%d)",
                        seat, tag, snake_id,
                    )
                    continue
                self._snake_id_by_seat[seat] = snake_id
                self._seat_by_snake_id[snake_id] = seat
                active_seats.add(seat)
                baseline = self._read_cpu_ns(tracker.container)
                tracker.name = tag
                tracker.last_cpu_ns = max(baseline, 0)
                tracker.step_budget_ns = self._per_step_budget_ns + self._initial_extra_ns
                tracker.accumulating_remaining_ns = self._accumulating_initial_ns
                tracker.wall_bank_remaining_ns = self._wall_accumulating_initial_ns
                tracker.step_start_wall_ns = now_wall
                tracker.step_start_cpu_ns = max(baseline, 0)
                tracker.responded_this_step = False
                tracker.response_wall_ns = 0
                tracker.in_startup = False
                log.info(
                    "cpu per-step monitoring: seat %d (snake_id=%d, tag=%s)",
                    seat, snake_id, tag,
                )

            # Seats with a tracker but no snake_id in snake_tags were dropped
            # during sim init (gRPC timeout, agent crash before SetInitData,
            # etc). Kill their containers now and tag the reason so the
            # runner can surface init failures upstream.
            for seat, tracker in self._trackers.items():
                if seat not in active_seats and not tracker.killed:
                    log.info(
                        "seat %d (tag=%s) absent from match start (init failure) — killing container",
                        seat, self._target_by_seat.get(seat, "?"),
                    )
                    tracker.killed = True
                    tracker.kill_reason = "init_failure"
                    self._kill_seat(seat)

        if self._thread is None or not self._thread.is_alive():
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="cpu-budget")
            self._thread.start()

    def notify_step(self, data: LoopStepData):
        # All keys here are SEATS. `data.alive_states` is keyed by snake_id
        # — translate via _snake_id_by_seat below.
        step_times: dict[int, float] = {}
        now_wall = time.monotonic_ns()
        with self._lock:
            wall_step_ns = max(0, now_wall - self._last_notify_step_wall_ns) if self._last_notify_step_wall_ns else 0
            self._last_notify_step_wall_ns = now_wall
            wall_step_ms = wall_step_ns / 1e6
            # First pass: snapshot CPU delta for each live tracker. We need
            # max_cpu_step before debiting the wall bank, so this is a two-pass
            # loop.
            cpu_deltas: dict[int, int] = {}
            for seat, tracker in self._trackers.items():
                if tracker.killed:
                    continue
                current = self._read_cpu_ns(tracker.container)
                if current < 0:
                    continue
                delta_ns = max(0, current - tracker.last_cpu_ns)
                cpu_deltas[seat] = delta_ns
                # Record the cpu reading so the second pass and downstream
                # state updates use a consistent value for this step.
                tracker.last_cpu_ns = current
            max_cpu_step_ns = max(cpu_deltas.values(), default=0)
            if wall_step_ns > 0 and max_cpu_step_ns > 0:
                self._step_history.append((wall_step_ns, max_cpu_step_ns))
            contention = self._contention_factor_locked()

            for seat, tracker in self._trackers.items():
                if tracker.killed:
                    continue
                current = tracker.last_cpu_ns  # already updated above
                delta_ns = cpu_deltas.get(seat, 0)
                step_times[seat] = delta_ns / 1e6
                self._exec_times_ms.setdefault(seat, []).append(delta_ns / 1e6)
                self._wall_step_times_ms.setdefault(seat, []).append(wall_step_ms)
                # Per-snake response wall for this step: time between step
                # start and when the sim acked this snake's decision. Falls
                # back to the full wall_step if notify_decision was never
                # received (shouldn't happen for an alive snake — the sim
                # wouldn't have moved on — but we'd rather over-charge in the
                # weird case than under-charge).
                if tracker.response_wall_ns and tracker.step_start_wall_ns:
                    response_wall_ns = max(
                        0, tracker.response_wall_ns - tracker.step_start_wall_ns
                    )
                else:
                    response_wall_ns = wall_step_ns
                tracker.step_budget_ns = self._per_step_budget_ns
                tracker.step_start_wall_ns = now_wall
                tracker.step_start_cpu_ns = current
                # Pending-set reset for the upcoming step. Done here (not in
                # notify_decision) so the window between this notify_step and
                # the next notify_decision is the snake's responsibility.
                tracker.responded_this_step = False
                tracker.response_wall_ns = 0

                if self._accumulating_enabled:
                    tracker.accumulating_remaining_ns += self._accumulating_step_ns - delta_ns
                    if tracker.accumulating_remaining_ns > self._accumulating_max_ns:
                        tracker.accumulating_remaining_ns = self._accumulating_max_ns
                    if tracker.accumulating_remaining_ns < 0:
                        log.warning(
                            "seat %d (tag=%s) over accumulating cpu budget "
                            "(bank %.3fs, used %.3fs this step) -> killing",
                            seat, tracker.name,
                            tracker.accumulating_remaining_ns / 1e9,
                            delta_ns / 1e9,
                        )
                        tracker.killed = True
                        tracker.kill_reason = "sustained"
                        self._kill_seat(seat)
                        continue

                if self._wall_accumulating_enabled and response_wall_ns > 0:
                    # excess_wall = response_wall - cpu_step * contention.
                    # response_wall is this snake's own time-to-respond, not
                    # the global wall_step, so a fast snake stuck behind a
                    # slow neighbour doesn't get billed for that neighbour's
                    # delay (its response_wall stays small).
                    expected_wall_ns = int(delta_ns * contention)
                    excess_ns = max(0, response_wall_ns - expected_wall_ns)
                    tracker.wall_bank_remaining_ns += self._wall_accumulating_step_ns - excess_ns
                    if tracker.wall_bank_remaining_ns > self._wall_accumulating_max_ns:
                        tracker.wall_bank_remaining_ns = self._wall_accumulating_max_ns
                    if tracker.wall_bank_remaining_ns < 0:
                        log.warning(
                            "seat %d (tag=%s) over sustained wall budget "
                            "(bank %.3fs, excess %.3fs this step, contention %.2f) -> killing",
                            seat, tracker.name,
                            tracker.wall_bank_remaining_ns / 1e9,
                            excess_ns / 1e9,
                            contention,
                        )
                        tracker.killed = True
                        tracker.kill_reason = "sustained_wall"
                        self._kill_seat(seat)
                        continue

                # alive_states is sim-side, keyed by snake_id. A seat that
                # never got a snake_id (rare: tracker exists but notify_start
                # never claimed this seat — already handled as init_failure
                # above) doesn't have a state to read here.
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

            # Test-match opponent cleanup. Once the dev agent is dead, give
            # the replay a few more frames of context so the user can see
            # what happened, then end the match by killing any opponents
            # that are still running. Enabled only when dev_seat is set
            # (constructor enforces this when kill_opp_after_dev_dies_steps
            # is non-None).
            if self._kill_opp_after_dev_dies_steps is not None:
                dev_tracker = self._trackers.get(self._dev_seat) if self._dev_seat is not None else None
                if dev_tracker is not None and dev_tracker.killed:
                    if self._dev_died_at_step is None:
                        self._dev_died_at_step = data.step
                    elif data.step - self._dev_died_at_step >= self._kill_opp_after_dev_dies_steps:
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

    def notify_decision(self, snake_id: int):
        """Called by the sim (via the socket observable) the moment a snake's
        decision arrives back at the sim, before notify_step fires for the
        step. Removes the snake from the wall-clock guards' kill set for the
        remainder of this step — a snake that has already answered cannot be
        what's holding up the sim, no matter how big the global wall_step
        gets thanks to a slow neighbour.

        Sim sends snake_id; we look up the matching seat via notify_start's
        mapping. A snake_id with no seat mapping shouldn't happen post
        notify_start but is harmless — just ignore."""
        now_wall = time.monotonic_ns()
        with self._lock:
            seat = self._seat_by_snake_id.get(snake_id)
            if seat is None:
                return
            tracker = self._trackers.get(seat)
            if tracker is None or tracker.killed:
                return
            tracker.responded_this_step = True
            tracker.response_wall_ns = now_wall

    def get_exec_times(self) -> dict[int, list[float]]:
        """Return accumulated per-step CPU times (ms) keyed by seat."""
        with self._lock:
            return {k: list(v) for k, v in self._exec_times_ms.items()}

    def get_wall_step_times(self) -> dict[int, list[float]]:
        """Per-seat wall time between consecutive notify_step events (ms).

        Values are globally the same at any given step (it's the sim's
        cycle time), but the per-seat shape matches get_exec_times(): a
        seat's list ends at the step where it died. Seat S's
        wall_step_times[S][i] aligns with exec_times[S][i].
        """
        with self._lock:
            return {k: list(v) for k, v in self._wall_step_times_ms.items()}

    def get_seat_by_snake_id(self) -> dict[int, int]:
        """Sim-side snake_id → runner-side seat. Empty until notify_start
        has fired. Used to give the frontend a stable identifier for
        coloring (seat) while the replay/sim still references snakes by
        snake_id."""
        with self._lock:
            return dict(self._seat_by_snake_id)

    def get_init_failed_seats(self) -> list[int]:
        """Seats whose containers were killed because they never appeared in
        the sim's notify_start (gRPC init timeout, agent crash before
        SetInitData, etc). Used by the runner to merge into
        MatchResult.init_failed_seats so daemons can quarantine the
        corresponding submitted images."""
        with self._lock:
            return [seat for seat, t in self._trackers.items()
                    if t.kill_reason == "init_failure"]

    def get_kill_reason(self, seat: int) -> str | None:
        """Why the given seat's container was killed, if it was. One of
        "startup_cpu", "per_step", "sustained", "wall_clock", "sustained_wall",
        "init_failure", "dead", "post_dev_cleanup", or None if the agent
        ran to clean completion / is still alive."""
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
            "wall_clock_psi_scale_k": self._wall_clock_psi_scale_k,
            "wall_clock_fallback_contention": self._wall_clock_fallback_contention,
            "wall_clock_max_contention": self._wall_clock_max_contention,
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
                # Compute contention + wall budget once per iteration — same
                # for every tracker, and reading PSI N times per cycle is
                # wasteful. Contention is also used by the projected
                # sustained-wall check below.
                contention = (
                    self._contention_factor_locked()
                    if self._wall_clock_enabled else 1.0
                )
                wall_budget_ns = (
                    max(
                        self._wall_clock_hard_floor_ns,
                        int(self._per_step_budget_ns * contention * self._wall_clock_safety_factor),
                    )
                    if self._wall_clock_enabled else 0
                )
                for seat, tracker in self._trackers.items():
                    if tracker.killed:
                        continue

                    current = self._read_cpu_ns(tracker.container)
                    if current < 0:
                        continue

                    used_this_step = current - tracker.last_cpu_ns
                    if used_this_step > tracker.step_budget_ns:
                        # Wall time since the budget window opened. cpu / wall
                        # is the seat's *busy ratio* — close to 1 means tight
                        # loop (real over-budget event), far below 1 means the
                        # agent was idle for most of the window. The latter is
                        # only possible if notify_step has been delayed and
                        # the cpu has accumulated across multiple intended
                        # steps, so the per-step kill is unfair. The
                        # accumulating cpu budget catches sustained abuse on
                        # the next notify_step instead.
                        wall_since_step = (
                            time.monotonic_ns() - tracker.step_start_wall_ns
                            if tracker.step_start_wall_ns else 0
                        )
                        busy_ratio = (
                            used_this_step / wall_since_step
                            if wall_since_step > 0 else 1.0
                        )
                        if busy_ratio >= _PER_STEP_KILL_BUSY_RATIO:
                            log.warning(
                                "seat %d (tag=%s) over cpu budget: used %.3fs cpu "
                                "in %.3fs wall / %.3fs budget -> killing",
                                seat, tracker.name,
                                used_this_step / 1e9,
                                wall_since_step / 1e9,
                                tracker.step_budget_ns / 1e9,
                            )
                            tracker.killed = True
                            tracker.kill_reason = "startup_cpu" if tracker.in_startup else "per_step"
                            self._kill_seat(seat)
                            continue
                        # Wall ≫ cpu — skip the kill, wait for the next
                        # notify_step (accumulating budget will catch it
                        # if the agent is genuinely over the long-run rate).
                        log.debug(
                            "seat %d (tag=%s) over per-step cpu (%.3fs cpu / "
                            "%.3fs wall, ratio %.2f) but window stretched "
                            "(notify_step delayed) — not killing",
                            seat, tracker.name,
                            used_this_step / 1e9, wall_since_step / 1e9,
                            busy_ratio,
                        )

                    # Wall-clock guard: catches agents that block the sim past
                    # the wall-clock budget. Only considers seats the sim is
                    # still waiting on (notify_decision has not arrived for
                    # this step) — an agent that already answered cannot be
                    # the cause of a stuck sim, even if global wall is huge
                    # because a neighbour is hanging.
                    if (
                        self._wall_clock_enabled
                        and not tracker.in_startup
                        and not tracker.responded_this_step
                        and tracker.step_start_wall_ns
                    ):
                        wall_elapsed = time.monotonic_ns() - tracker.step_start_wall_ns
                        cpu_in_window = current - tracker.step_start_cpu_ns
                        if wall_elapsed > wall_budget_ns:
                            # Disambiguate the kill reason for the dev
                            # console: cpu way over per-step budget means a
                            # single slow step holding the sim; otherwise
                            # the agent is presumed sleeping / blocked.
                            if cpu_in_window > tracker.step_budget_ns:
                                log.warning(
                                    "seat %d (tag=%s) stuck: cpu %.3fs > step "
                                    "budget %.3fs in wall %.3fs > %.3fs -> "
                                    "killing (single slow step holding sim)",
                                    seat, tracker.name,
                                    cpu_in_window / 1e9,
                                    tracker.step_budget_ns / 1e9,
                                    wall_elapsed / 1e9, wall_budget_ns / 1e9,
                                )
                                tracker.kill_reason = "per_step"
                            else:
                                log.warning(
                                    "seat %d (tag=%s) wall-clock %.3fs > budget "
                                    "%.3fs, cpu only %.3fs -> killing "
                                    "(sleeping/hung, still pending)",
                                    seat, tracker.name,
                                    wall_elapsed / 1e9, wall_budget_ns / 1e9,
                                    cpu_in_window / 1e9,
                                )
                                tracker.kill_reason = "wall_clock"
                            tracker.killed = True
                            self._kill_seat(seat)
                            continue

                    # Projected sustained CPU kill: replicates the bank
                    # computation that runs at notify_step but uses the
                    # in-progress cpu delta. If the bank would go negative
                    # at end-of-step (current bank + refill − used_so_far),
                    # kill now instead of waiting for the agent to finish
                    # its move — otherwise the agent's response for this
                    # step makes it into the sim's state and we end up with
                    # a "ghost" frame after the kill.
                    #
                    # Gated on responded_this_step the same way as the wall
                    # guards: once the agent has answered, its move is
                    # already in the sim's state (no ghost frame to
                    # prevent) and `used_this_step` may include cpu from
                    # subsequent fast steps the runner hasn't processed a
                    # notify_step for yet — both reasons to leave the kill
                    # to the next notify_step's per-step debit.
                    if (
                        self._accumulating_enabled
                        and not tracker.in_startup
                        and not tracker.responded_this_step
                    ):
                        projected_bank = (
                            tracker.accumulating_remaining_ns
                            + self._accumulating_step_ns
                            - used_this_step
                        )
                        if projected_bank < 0:
                            log.warning(
                                "seat %d (tag=%s) projected over accumulating cpu "
                                "budget (bank %.3fs + refill %.3fs − used %.3fs "
                                "= %.3fs) -> killing (mid-step)",
                                seat, tracker.name,
                                tracker.accumulating_remaining_ns / 1e9,
                                self._accumulating_step_ns / 1e9,
                                used_this_step / 1e9,
                                projected_bank / 1e9,
                            )
                            tracker.killed = True
                            tracker.kill_reason = "sustained"
                            self._kill_seat(seat)
                            continue

                    # Projected sustained wall kill: same idea as the CPU
                    # projection, but for the wall bank. excess wall =
                    # wall − cpu * contention; if the bank would go negative
                    # given the in-progress wall window, kill now.
                    #
                    # Only consider seats that are still pending this step
                    # (no notify_decision yet) — an agent that already
                    # responded isn't accumulating response wall and isn't
                    # the cause of any remaining slowness.
                    if (
                        self._wall_accumulating_enabled
                        and not tracker.in_startup
                        and not tracker.responded_this_step
                        and tracker.step_start_wall_ns
                    ):
                        wall_so_far = time.monotonic_ns() - tracker.step_start_wall_ns
                        expected_wall_so_far = int(used_this_step * contention)
                        excess_so_far = max(0, wall_so_far - expected_wall_so_far)
                        projected_wall_bank = (
                            tracker.wall_bank_remaining_ns
                            + self._wall_accumulating_step_ns
                            - excess_so_far
                        )
                        if projected_wall_bank < 0:
                            log.warning(
                                "seat %d (tag=%s) projected over sustained wall "
                                "budget (bank %.3fs + refill %.3fs − excess "
                                "%.3fs = %.3fs, contention %.2f) -> killing "
                                "(mid-step)",
                                seat, tracker.name,
                                tracker.wall_bank_remaining_ns / 1e9,
                                self._wall_accumulating_step_ns / 1e9,
                                excess_so_far / 1e9,
                                projected_wall_bank / 1e9,
                                contention,
                            )
                            tracker.killed = True
                            tracker.kill_reason = "sustained_wall"
                            self._kill_seat(seat)
                            continue

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

    def _kill_seat(self, seat_id: int) -> None:
        """Send SIGKILL to the seat's container. SIGKILL is uncatchable
        in user space, so the snake's process is torn down regardless of
        any signal handlers it tried to install; the kernel closes its
        TCP sockets on the way out, which lets the sim's gRPC stream
        error normally and the sim then exits when no batches remain.
        Async at the docker layer (~10–50 ms), so the snake may answer
        1–2 more in-flight steps before its process actually dies."""
        tracker = self._trackers.get(seat_id)
        if tracker is None:
            return
        try:
            tracker.container.kill()
        except NotFound:
            pass  # already gone — goal state met
        except APIError as e:
            # 409 Conflict = "container is not running": it already exited,
            # which is exactly what we wanted. Anything else is genuinely
            # unexpected. If the docker SDK's exception shape changes and
            # .response.status_code is no longer present, we *want* the
            # AttributeError to surface so we know to update the pin.
            if e.response.status_code == 409:
                log.debug("container %s already stopped", tracker.container.name)
            else:
                log.warning("failed to kill container %s: %s", tracker.container.name, e)
