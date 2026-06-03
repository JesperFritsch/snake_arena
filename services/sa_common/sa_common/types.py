

from dataclasses import dataclass
from pydantic import BaseModel, model_validator, Field

from snake_sim.analyze.scripts.run_analyzer import RunAnalysis


@dataclass
class MatchResult:
    success: bool
    sim_logs: str = ""
    agent_logs: dict[str, str] | None = None
    tags_to_names: dict[str, str] | None = None
    run_analysis: RunAnalysis | None = None
    # Per-step stdout chunks for the dev agent (seat 0), split on the harness
    # step separator. Used to build the test-match console view.
    dev_agent_step_logs: list[str] | None = None
    # Per-step CPU times (ms) keyed by seat. seat → [ms per step].
    exec_times: dict[int, list[float]] | None = None
    # Per-step sim cycle wall (ms), per seat. Sourced from the sim's own
    # `LoopStepData.total_time` so it is lag-immune (not derived from
    # manager-side receive-time deltas). Globally the same at any given
    # step but shaped like exec_times so each seat's list ends when it
    # dies. seat → [ms].
    wall_step_times: dict[int, list[float]] | None = None
    # CPU budget config (seconds) that was in force for this match.
    budgets: dict[str, float] | None = None
    # Kill reason per seat: "per_step" | "sustained" | "wall_clock" |
    # "sustained_wall" | "startup_cpu" | "init_failure" | "dead" | None
    kill_reasons: dict[int, str | None] | None = None
    # Seats whose gRPC server never came up before the match started, as
    # detected by the pre-sim probe. Daemons map these back to project_ids
    # and quarantine those submitted images so the matchmaker stops picking
    # them until the next submit.
    init_failed_seats: list[int] | None = None
    # Sim-assigned snake_id → runner-assigned seat. Populated once the sim
    # publishes its snake_tags at notify_start. Lets downstream consumers
    # (bundle, frontend) join sim-side data (replay, alive_states) with
    # runner-side data (participants, exec_times) without hardcoding the
    # snake_id == seat assumption.
    seat_by_snake_id: dict[int, int] | None = None
    # Captured stream of the `runner` logger tree for this match (match.py +
    # agent_container_manager + cpu-budget poll thread). Populated by
    # run_match() right before it returns; consumed by assemble_bundle() to
    # ship runner_logs.txt in the match bundle for post-mortems.
    runner_logs: str = ""
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