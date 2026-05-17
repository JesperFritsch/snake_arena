# services/orchestrator/orchestrator/agents.py
"""Resolve a list of submission IDs into the setup needed to run + persist a match.

The runner needs `list[AgentSpec]` (image + dns name). The DB layer needs
mappings from each agent's name back to its project/submission/seat. Both
are built here in one pass so the orchestrator's main loop stays simple.
"""
from __future__ import annotations

from dataclasses import dataclass

import psycopg

from runner.match import AgentSpec
from sa_common.db.submissions import get_submission


@dataclass(frozen=True)
class AgentSetup:
    """Everything the orchestrator needs about the agents for a match."""
    specs: list[AgentSpec]                       # → runner
    project_by_name: dict[str, int]              # → build_participants
    submission_by_name: dict[str, int]           # → build_participants
    seat_by_name: dict[str, int]                 # → build_participants


class SetupError(Exception):
    """Raised when a job's submissions can't be turned into a runnable setup."""


def resolve_agents(
    conn: psycopg.Connection,
    submission_ids: list[int],
) -> AgentSetup:
    """Look up submissions and build the AgentSetup.

    Agent names are assigned positionally as 'agent_0', 'agent_1', ... so the
    seat for each name is just its index. The same index propagates through
    every mapping, which keeps mirror matches (same submission appearing
    twice) unambiguous.

    Raises:
        SetupError: if a submission doesn't exist or isn't ready to play.
    """
    if not submission_ids:
        raise SetupError("job has no submission_ids")

    specs: list[AgentSpec] = []
    project_by_name: dict[str, int] = {}
    submission_by_name: dict[str, int] = {}
    seat_by_name: dict[str, int] = {}

    for i, sub_id in enumerate(submission_ids):
        # NOTE: get_submission pulls code_archive, which we don't need here.
        # Worth adding a get_submission_meta_by_id to submissions.py if
        # match dispatch volume ever matters.
        sub = get_submission(conn, sub_id)
        if sub is None:
            raise SetupError(f"submission {sub_id} not found")
        if sub.status != "ready":
            raise SetupError(
                f"submission {sub_id} is not ready (status={sub.status!r})"
            )

        name = f"agent_{i}"
        specs.append(AgentSpec(image=sub.image_tag, name=name))
        project_by_name[name] = sub.project_id
        submission_by_name[name] = sub.id
        seat_by_name[name] = i

    return AgentSetup(
        specs=specs,
        project_by_name=project_by_name,
        submission_by_name=submission_by_name,
        seat_by_name=seat_by_name,
    )