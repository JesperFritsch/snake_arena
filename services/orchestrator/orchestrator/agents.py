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
from sa_common.db.projects import get_project_meta


@dataclass(frozen=True)
class AgentSetup:
    """Everything the orchestrator needs about the agents for a match."""
    specs: list[AgentSpec]                       # → runner
    project_by_name: dict[str, int]              # → build_participants
    version_by_name: dict[str, int]              # → build_participants
    seat_by_name: dict[str, int]                 # → build_participants


class SetupError(Exception):
    """Raised when a job's submissions can't be turned into a runnable setup."""


def resolve_agents(conn, project_ids: list[int]) -> AgentSetup:
    if not project_ids:
        raise SetupError("job has no project_ids")

    specs, project_by_name, version_by_name, seat_by_name = [], {}, {}, {}

    for i, project_id in enumerate(project_ids):
        meta = get_project_meta(conn, project_id)
        if meta is None:
            raise SetupError(f"project {project_id} not found")
        if meta.submitted_version == 0:
            raise SetupError(f"project {project_id} has never been submitted")
        if meta.submitted_image_tag is None:
            # Defensive — the CHECK constraint prevents this, but if it ever
            # happens we want a clear error rather than a None being passed
            # to docker run.
            raise SetupError(f"project {project_id} has no submitted_image_tag")

        name = f"agent_{i}"
        specs.append(AgentSpec(image=meta.submitted_image_tag, name=name))
        project_by_name[name] = meta.id
        version_by_name[name] = meta.submitted_version
        seat_by_name[name] = i

    return AgentSetup(
        specs=specs,
        project_by_name=project_by_name,
        version_by_name=version_by_name,
        seat_by_name=seat_by_name,
    )