# services/api/api/schemas.py
"""Request and response shapes.

Where a `sa_common` dataclass is already a clean, byte-free view (ProjectMeta,
MatchJob, BuildJob, Match), we let FastAPI serialise it directly and only
define schemas here for request bodies and for composite responses that don't
map to a single dataclass. This keeps the OpenAPI contract — which the frontend
codegens against — anchored to the existing types.

Project code crosses the wire as a *file structure*, never a tarball: a list
of {path, content, encoding}. The API packs that into a .tar.gz for storage
and unpacks it back on read, so the browser never deals with archive bytes.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from sa_common.db.projects import ProjectSource
from sa_common.types import SimArgs


# ---- project files (the wire form of project code) ------------------------

class ProjectFile(BaseModel):
    """One file in a project's tree.

    `path` is a forward-slash relative path (directories are implied by it,
    e.g. "src/agent.py"). `content` is UTF-8 text by default; set
    encoding="base64" for binary files.
    """
    path: str = Field(min_length=1, max_length=255)
    content: str = ""
    encoding: Literal["utf-8", "base64"] = "utf-8"


class ProjectFiles(BaseModel):
    files: list[ProjectFile]


# ---- requests -------------------------------------------------------------

class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    language: str = Field(min_length=1, max_length=64)
    source: ProjectSource = "browser"
    # Required (non-empty) for browser projects; must be empty for
    # external_image projects. Enforced in the route for a clean 400.
    files: list[ProjectFile] = Field(default_factory=list)


class MatchJobCreate(BaseModel):
    project_ids: list[int] = Field(min_length=1, max_length=8)
    sim_args: SimArgs


class TestMatchCreate(BaseModel):
    player_project_id: int
    opponent_project_ids: list[int] = Field(default_factory=list, max_length=4)
    sim_args: SimArgs


# ---- responses ------------------------------------------------------------

class UserOut(BaseModel):
    id: int
    email: str
    display_name: str


class SubmitResult(BaseModel):
    submitted_version: int


class PublicProjectSummary(BaseModel):
    id: int
    name: str
    language: str
    submitted_version: int
    submitted_at: Any
    user_display_name: str


class ParticipantOut(BaseModel):
    seat: int
    project_id: int
    project_version: int
    final_length: int | None
    fatal_step: int | None
    survival_rank: int | None
    killed_by_budget: bool
    metrics: dict[str, Any]


class MatchDetail(BaseModel):
    id: int
    match_uuid: str
    status: str
    mode: str
    sim_args: dict[str, Any]
    started_at: Any
    finished_at: Any | None
    replay_r2_key: str | None
    error: str | None
    participants: list[ParticipantOut]