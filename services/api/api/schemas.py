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

from pydantic import BaseModel, Field, model_validator

from sa_common.db.projects import ProjectSource
from sa_common.types import SimArgs


# Test matches run in the browser preview, so the grid is capped to keep
# replays small and renderable.
TEST_MATCH_MIN_GRID = 5
TEST_MATCH_MAX_GRID = 20


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
    name: str = Field(min_length=1, max_length=32)
    language: str = Field(min_length=1, max_length=64)
    source: ProjectSource = "browser"
    # Required (non-empty) for browser projects; must be empty for
    # external_image projects. Enforced in the route for a clean 400.
    files: list[ProjectFile] = Field(default_factory=list)


class TestMatchCreate(BaseModel):
    player_project_id: int
    opponent_project_ids: list[int] = Field(default_factory=list, max_length=4)
    sim_args: SimArgs

    @model_validator(mode="after")
    def _cap_grid(self) -> "TestMatchCreate":
        dims = (self.sim_args.grid_width, self.sim_args.grid_height)
        if any(d is not None for d in dims):
            for d in dims:
                if d is None or d < TEST_MATCH_MIN_GRID or d > TEST_MATCH_MAX_GRID:
                    raise ValueError(
                        f"grid_width and grid_height must be between "
                        f"{TEST_MATCH_MIN_GRID} and {TEST_MATCH_MAX_GRID}"
                    )
        return self


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
    mode_id: int | None       # NULL for test matches
    sim_args: dict[str, Any]
    started_at: Any
    finished_at: Any | None
    bundle_key: str | None
    error: str | None
    participants: list[ParticipantOut]


class RankedMatchParticipant(BaseModel):
    seat: int
    project_id: int
    project_name: str
    final_length: int | None
    survival_rank: int | None
    metrics: dict[str, Any]


class RankedMatchSummary(BaseModel):
    id: int
    match_uuid: str
    status: str
    mode_id: int | None
    started_at: Any
    finished_at: Any | None
    bundle_key: str | None
    participants: list[RankedMatchParticipant]


class LeaderboardEntry(BaseModel):
    rank: int
    project_id: int
    project_name: str
    language: str
    user_display_name: str
    matches_played: int
    avg_score: float
    best_score: float
    avg_rank: float
    avg_length: float | None


class OverallLeaderboardEntry(BaseModel):
    rank: int
    project_id: int
    project_name: str
    language: str
    user_display_name: str
    overall_score: float          # 0..100, mean of per-mode normalised scores
    total_matches: int
    modes_played: int


class ModeOut(BaseModel):
    id: int
    slug: str
    name: str
    description: str | None
    participant_count: int
    sim_args: dict[str, Any]
    map_slug: str | None
    budget_ms: float
    target_matches_per_version: int
    enabled: bool