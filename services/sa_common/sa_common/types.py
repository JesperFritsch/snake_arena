

from pathlib import Path
from dataclasses import dataclass
from pydantic import BaseModel, model_validator, Field

from snake_sim.analyze.scripts.run_analyzer import RunAnalysis


@dataclass
class MatchResult:
    success: bool
    sim_logs: str | None = None
    agent_logs: dict[str, str] | None = Field(default_factory={})
    tags_to_names: dict[str, str] | None = Field(default_factory={})
    replay_path: Path | None = None
    run_analysis: RunAnalysis | None = None 
    error: str | None = None


@dataclass
class BuildResult:
    success: bool
    duration_s: float
    image_tag: str | None = None
    build_logs: str | None = None
    error: str | None = None


class SimArgs(BaseModel):
    food: int
    grid_height: int | None = None
    grid_width: int | None = None
    map: str | None = None

    @model_validator(mode="after")
    def validate_dims(self):
        has_height = self.grid_height is not None
        has_width = self.grid_width is not None

        if has_height != has_width:
            raise ValueError(
                "Provide either both grid_height and grid_width or neither"
            )

        if has_height and self.map is not None:
            raise ValueError("Can't have both map and dimensions")

        return self

    def to_args(self) -> list[str]:
        args = []

        for key, value in self.model_dump(exclude_none=True).items():
            key = key.replace("_", "-")

            args.append(f"--{key}")
            args.append(str(value))

        return args

    @classmethod
    def from_args(cls, args: list[str]) -> "SimArgs":
        if len(args) % 2 != 0:
            raise ValueError("Arguments must be key/value pairs")

        data = {}

        for i in range(0, len(args), 2):
            key = args[i]
            value = args[i + 1]

            if not key.startswith("--"):
                raise ValueError(f"Invalid argument name: {key}")

            key = key[2:].replace("-", "_")

            data[key] = value

        return cls.model_validate(data)
    

class ParticipantRow(BaseModel):
    seat: int
    project_id: int
    project_version: int
    final_length: int | None = None
    fatal_step: int | None = None
    survival_rank: int | None = None
    killed_by_budget: bool = False
    metrics: dict = Field(default_factory=dict)