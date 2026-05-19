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
    """
    if not result.run_analysis:
        # No analysis: record participation with what we know from setup.
        # Per-snake outcomes (fatal_step, survival_rank, final_length) stay None.
        log.warning("building participants without run_analysis (best-effort)")
        return [
            ParticipantRow(
                seat=seat_by_agent_name[name],
                project_id=project_by_agent_name[name],
                project_version=version_by_agent_name[name],
            )
            for name in result.agent_logs.keys()
            if name in seat_by_agent_name  # skip anything we didn't set up
        ]

    analysis = result.run_analysis
    tags_to_names = result.tags_to_names
    snake_tags = analysis.env_meta_data.snake_tags  # snake_id -> agent_name
    fatal_steps = analysis.fatal_steps
    final_step = analysis.final_step_idx
    snake_ids = analysis.snake_ids

    def death_order_key(sid: int) -> int:
        # Snakes still alive at end get a value beyond any real fatal_step
        # so they rank ahead of those that died earlier.
        return fatal_steps.get(sid, final_step + 1)

    ranked = sorted(snake_ids, key=death_order_key, reverse=True)
    rank_of_snake = {sid: i + 1 for i, sid in enumerate(ranked)}

    participants: list[ParticipantRow] = []
    for sid in snake_ids:
        agent_tag = snake_tags.get(sid)
        agent_name = tags_to_names.get(agent_tag)
        print(agent_name, seat_by_agent_name)
        if agent_name is None or agent_name not in seat_by_agent_name:
            log.warning(
                "snake_id %s has no setup mapping (tag=%r); skipping",
                sid,
                agent_name,
            )
            continue

        participants.append(
            ParticipantRow(
                seat=seat_by_agent_name[agent_name],
                project_id=project_by_agent_name[agent_name],
                project_version=version_by_agent_name[agent_name],
                final_length=None,  # TODO: pull from analysis once API confirmed
                fatal_step=fatal_steps.get(sid),
                survival_rank=rank_of_snake[sid],
                killed_by_budget=False,  # TODO: surface from CpuBudgetObserver
                metrics={},
            )
        )

    return participants