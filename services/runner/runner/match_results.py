# services/runner/runner/match_results.py
"""Transform a MatchResult into rows the DB layer can store.

This is runner-owned logic. The runner knows which agent_name corresponds
to which (project, submission), and how to interpret a SimAnalysis. The DB
layer just persists whatever this module produces.
"""
from __future__ import annotations

import logging

from sa_common.types import MatchResult, ParticipantRow

log = logging.getLogger(__name__)

_BUDGET_KILL_REASONS = {"per_step", "sustained", "wall_clock", "sustained_wall", "startup_cpu"}


def _trapping_counts(
    traps_mapping: dict,
    tail_visible_phases: dict,
    snake_ids: list[int],
) -> dict[int, int]:
    """Count, per snake_id, the number of trap events the snake set that
    actually killed the trapped snake(s). The analyzer also registers traps
    that the trapped snake later escapes from, so a raw count overcredits.
    A trap only counts if every snake it caught never regained tail
    visibility after the trap step — i.e. they stayed enclosed in their
    progressively filling region until death.

    Each qualifying trap event contributes 1 to each trapper — not weighted
    by how many snakes it killed.

    `traps_mapping` is the analysis blob's `{step_idx: [{trapped_ids,
    trapping_ids}, ...]}` shape. `tail_visible_phases` is the analysis
    blob's `{snake_id: [{start_step_idx, end_step_idx, tail_visible}, ...]}`
    shape. After bundle round-trip both may have string keys / lists in
    place of sets, hence the tolerant access. Snakes with no tail_visible
    phase data are treated as "always visible" so unverifiable traps don't
    get credited.
    """
    counts = {sid: 0 for sid in snake_ids}

    # Per snake_id, the latest step at which tail_visible=True held. A
    # trap at step S is a kill only when last_visible_step <= S — i.e. the
    # snake never saw its tail again afterwards.
    last_visible_step: dict[int, int] = {}
    for sid_key, phases in (tail_visible_phases or {}).items():
        try:
            sid = int(sid_key)
        except (TypeError, ValueError):
            continue
        latest = -1
        for phase in phases or ():
            if not phase.get("tail_visible"):
                continue
            end = phase.get("end_step_idx")
            if end is None:
                continue
            end = int(end)
            if end > latest:
                latest = end
        last_visible_step[sid] = latest

    NEVER_LOST_TAIL = float("inf")  # missing data → don't credit the trap

    for step_key, traps_at_step in (traps_mapping or {}).items():
        try:
            trap_step = int(step_key)
        except (TypeError, ValueError):
            continue
        for trap in traps_at_step:
            trapped_ids = [int(t) for t in trap.get("trapped_ids", ())]
            if not trapped_ids:
                continue
            if not all(
                last_visible_step.get(tid, NEVER_LOST_TAIL) <= trap_step
                for tid in trapped_ids
            ):
                continue
            for sid in trap.get("trapping_ids", ()):
                sid = int(sid)
                if sid in counts:
                    counts[sid] += 1
    return counts


def build_participants(
    result: MatchResult,
    project_by_agent_name: dict[str, int],
    version_by_agent_name: dict[str, int],
    seat_by_agent_name: dict[str, int],
) -> list[ParticipantRow]:
    """Convert a MatchResult into ParticipantRow values for the DB.

    Args:
        result: the runner's MatchResult, possibly without analysis.
        project_by_agent_name: runner setup info — which project each agent
            represents.
        version_by_agent_name: runner setup info — which project version each
            agent was built from.
        seat_by_agent_name: runner setup info — the position the runner
            assigned to each agent in this match. Used for PK disambiguation,
            especially in mirror matches.

    Returns:
        A list of ParticipantRow, one per participating agent. May be empty
        if the match has no usable participation data at all.

    Each row's `metrics` carries the runner-only base values:
    `start_length`, `steps_alive`, `avg_cpu_ms`, `trapping_count`.
    `final_length` and `survival_rank` live as native columns on the row;
    the scoring layer reads from columns + metrics. The runner stays a
    pure data producer.
    """
    # run_analysis is required — a "successful" match without it would silently
    # produce participant rows with no per-snake outcomes (length/rank/step all
    # None), which the leaderboard then can't score. Callers must catch this if
    # they want to record partial state; the runner_daemon currently fails the
    # job, which is the right call.
    if result.run_analysis is None:
        raise ValueError("build_participants requires result.run_analysis")
    if result.exec_times is None:
        raise ValueError("build_participants requires result.exec_times")
    analysis = result.run_analysis
    tags_to_names = result.tags_to_names
    snake_tags = analysis.env_meta_data.snake_tags  # snake_id -> agent_name
    fatal_steps = analysis.fatal_steps
    final_step = analysis.final_step_idx
    snake_ids = analysis.snake_ids
    start_lengths = analysis.start_lengths
    final_lengths = analysis.final_lengths

    def death_order_key(sid: int) -> int:
        # Snakes still alive at end get a value beyond any real fatal_step
        # so they rank ahead of those that died earlier.
        return fatal_steps.get(sid, final_step + 1)

    ranked = sorted(snake_ids, key=death_order_key, reverse=True)
    rank_of_snake = {sid: i + 1 for i, sid in enumerate(ranked)}

    # `traps_mapping` and `tail_visible_phases` are dataclass attrs on
    # RunAnalysis. We pass them through `to_dict()` shape (the same shape
    # the scorer sees from the bundle) so both producers feed identical
    # input into _trapping_counts.
    analysis_dict = analysis.to_dict()
    traps_dict = analysis_dict.get("traps_mapping", {})
    tail_phases = analysis_dict.get("tail_visible_phases", {})
    trapping_counts = _trapping_counts(traps_dict, tail_phases, snake_ids)

    participants: list[ParticipantRow] = []
    for sid in snake_ids:
        agent_tag = snake_tags.get(sid)
        agent_name = tags_to_names.get(agent_tag)
        if agent_name is None or agent_name not in seat_by_agent_name:
            log.warning(
                "snake_id %s has no setup mapping (tag=%r); skipping",
                sid,
                agent_name,
            )
            continue

        seat = seat_by_agent_name[agent_name]
        kill_reason = result.kill_reasons.get(seat) if result.kill_reasons else None
        final_length = final_lengths.get(sid)
        start_length = start_lengths.get(sid)
        seat_exec = result.exec_times.get(seat)
        if final_length is None:
            raise ValueError(
                f"snake_id={sid} (agent={agent_name!r}, seat={seat}) "
                f"missing from analysis.final_lengths"
            )
        if start_length is None:
            raise ValueError(
                f"snake_id={sid} (agent={agent_name!r}, seat={seat}) "
                f"missing from analysis.start_lengths"
            )
        if not seat_exec:
            raise ValueError(
                f"seat={seat} (agent={agent_name!r}) has no exec_times — "
                f"the agent took zero steps but build_participants was called anyway"
            )

        steps_alive = len(seat_exec)
        avg_cpu_ms = sum(seat_exec) / steps_alive

        # final_length and survival_rank are not duplicated into metrics —
        # the columns are canonical.
        metrics = {
            "start_length":   float(start_length),
            "steps_alive":    float(steps_alive),
            "avg_cpu_ms":     avg_cpu_ms,
            "trapping_count": float(trapping_counts[sid]),
        }

        participants.append(
            ParticipantRow(
                seat=seat,
                project_id=project_by_agent_name[agent_name],
                project_version=version_by_agent_name[agent_name],
                final_length=final_length,
                fatal_step=fatal_steps.get(sid),
                survival_rank=rank_of_snake[sid],
                killed_by_budget=kill_reason in _BUDGET_KILL_REASONS,
                metrics=metrics,
            )
        )

    return participants
