# services/orchestrator/orchestrator/bundle.py
"""Assemble and read match bundles (zip).

Bundle contents:
  replay.json         – the recorded run (JSONL of sim messages)
  analysis.json       – run_analyzer output ({} if analysis didn't run)
  agent_logs.json     – dev-agent per-step stdout (test matches only)
  exec_times.json     – per-snake per-step CPU times in ms
  wall_step_times.json– wall time between notify_step events, in ms
                        (one entry per step, shared across snakes)
  budgets.json        – CPU budget config (seconds) that was in force for this run

Both assemble_bundle (writer) and read_bundle (reader) live here so that any
change to the bundle format is a single-file edit.
"""
from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path

from snake_sim.analyze.scripts.run_analyzer import RunAnalysis


@dataclass(slots=True)
class BundleContents:
    """All six files in a bundle, parsed.

    replay, analysis, exec_times, wall_step_times, and budgets are always
    present for ranked matches — assemble_bundle writes them unconditionally.
    agent_logs is only present for test matches (the writer skips it when
    dev_step_logs is None). Missing required files raise from read_bundle().
    """
    replay: list[dict]                            # parsed replay.json JSONL — one sim message per element
    analysis: dict                                # run_analyzer output
    agent_logs: dict[str, list[str]] | None       # {"0": [step_log_str, ...]}; None for ranked matches
    exec_times: dict[int, list[float]]            # seat -> [cpu ms per step]
    wall_step_times: dict[int, list[float]]       # seat -> [wall ms per step], same shape as exec_times
    budgets: dict[str, float]                     # everything AgentContainerManager.get_budgets() wrote

    @property
    def budget_ms(self) -> float:
        """Per-step CPU budget in ms, derived from budgets['per_step_seconds']."""
        return float(self.budgets["per_step_seconds"]) * 1000


def read_bundle(bundle_bytes: bytes) -> BundleContents:
    """Parse a bundle zip into all of its component files.

    Raises if a required file is missing. agent_logs.json is the only
    optional file (test-match-only).
    """
    with zipfile.ZipFile(io.BytesIO(bundle_bytes)) as zf:
        names = set(zf.namelist())
        for required in (
            "replay.json", "analysis.json", "exec_times.json",
            "wall_step_times.json", "budgets.json",
        ):
            if required not in names:
                raise ValueError(f"bundle missing required file: {required}")

        replay_text = zf.read("replay.json").decode()
        replay = [json.loads(line) for line in replay_text.splitlines() if line.strip()]

        analysis = json.loads(zf.read("analysis.json"))
        budgets = json.loads(zf.read("budgets.json"))

        exec_times_raw = json.loads(zf.read("exec_times.json"))
        exec_times = {int(k): v for k, v in exec_times_raw.items()}

        wall_step_times_raw = json.loads(zf.read("wall_step_times.json"))
        wall_step_times = {int(k): v for k, v in wall_step_times_raw.items()}

        agent_logs = json.loads(zf.read("agent_logs.json")) if "agent_logs.json" in names else None

    return BundleContents(
        replay=replay,
        analysis=analysis,
        agent_logs=agent_logs,
        exec_times=exec_times,
        wall_step_times=wall_step_times,
        budgets=budgets,
    )


def assemble_bundle(
    replay_path: Path,
    run_analysis: RunAnalysis | None,
    dev_step_logs: list[str] | None = None,
    exec_times: dict[int, list[float]] | None = None,
    wall_step_times: dict[int, list[float]] | None = None,
    budgets: dict[str, float] | None = None,
) -> bytes:
    analysis_obj = run_analysis.to_dict() if run_analysis is not None else {}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        if replay_path.exists():
            zf.writestr("replay.json", replay_path.read_bytes())
        zf.writestr("analysis.json", json.dumps(analysis_obj).encode())
        if dev_step_logs is not None:
            zf.writestr("agent_logs.json", json.dumps({"0": dev_step_logs}).encode())
        if exec_times is not None:
            # String-keyed for JSON compatibility.
            zf.writestr("exec_times.json", json.dumps({str(k): v for k, v in exec_times.items()}).encode())
        if wall_step_times is not None:
            zf.writestr(
                "wall_step_times.json",
                json.dumps({str(k): v for k, v in wall_step_times.items()}).encode(),
            )
        if budgets is not None:
            zf.writestr("budgets.json", json.dumps(budgets).encode())
    return buf.getvalue()
