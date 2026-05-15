

from pathlib import Path
from dataclasses import dataclass

from snake_sim.analyze.scripts.run_analyzer import RunAnalysis


@dataclass
class MatchResult:
    success: bool
    sim_exit_code: int
    sim_logs: str
    agent_logs: dict[str, str]
    replay_path: Path | None = None
    run_analysis: RunAnalysis | None = None 
    error: str | None = None


@dataclass
class BuildResult:
    success: bool
    image_tag: str | None
    build_logs: str
    duration_s: float
    error: str | None = None
