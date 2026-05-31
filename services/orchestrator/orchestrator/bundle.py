# services/orchestrator/orchestrator/bundle.py
"""Assemble and read match bundles (zip).

Bundle contents:
  replay.json         – the recorded run (JSONL of sim messages)
  analysis.json       – run_analyzer output ({} if analysis didn't run)
  agent_logs.json     – dev-agent per-step stdout (test matches only)
  exec_times.json     – per-seat per-step CPU times in ms
  wall_step_times.json– wall time between notify_step events, in ms
                        (one entry per step, per seat — same shape as exec_times)
  budgets.json        – CPU budget config (seconds) that was in force for this run
  sim_logs.txt        – raw stdout/stderr of the sim container (useful for
                        post-mortem of init failures and crashes)
  seat_by_snake_id.json – {"<snake_id>": seat}; lets the player join sim-side
                        identifiers (snake_id in replay) with runner-side
                        identifiers (seat in participants/exec_times) without
                        assuming the two coincide.

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
    sim_logs: str                                 # raw sim container stdout/stderr
    seat_by_snake_id: dict[int, int]              # sim snake_id -> runner seat; empty if notify_start never fired

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
            "wall_step_times.json", "budgets.json", "sim_logs.txt",
            "seat_by_snake_id.json",
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

        seat_by_snake_id_raw = json.loads(zf.read("seat_by_snake_id.json"))
        seat_by_snake_id = {int(k): int(v) for k, v in seat_by_snake_id_raw.items()}

        agent_logs = json.loads(zf.read("agent_logs.json")) if "agent_logs.json" in names else None
        sim_logs = zf.read("sim_logs.txt").decode(errors="replace")

    return BundleContents(
        replay=replay,
        analysis=analysis,
        agent_logs=agent_logs,
        exec_times=exec_times,
        wall_step_times=wall_step_times,
        budgets=budgets,
        sim_logs=sim_logs,
        seat_by_snake_id=seat_by_snake_id,
    )


def assemble_bundle(
    replay_path: Path,
    run_analysis: RunAnalysis | None,
    sim_logs: str,
    dev_step_logs: list[str] | None = None,
    exec_times: dict[int, list[float]] | None = None,
    wall_step_times: dict[int, list[float]] | None = None,
    budgets: dict[str, float] | None = None,
    seat_by_snake_id: dict[int, int] | None = None,
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
        # Required file even when empty — read_bundle insists on it. An
        # empty mapping is legitimate (notify_start never fired, so we
        # never learned any snake_id assignments).
        zf.writestr(
            "seat_by_snake_id.json",
            json.dumps({str(k): v for k, v in (seat_by_snake_id or {}).items()}).encode(),
        )
        zf.writestr("sim_logs.txt", sim_logs.encode(errors="replace"))
    return buf.getvalue()
