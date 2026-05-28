# services/orchestrator/orchestrator/bundle.py
"""Assemble and read match bundles (zip).

Bundle contents:
  replay.json     – the recorded run (JSONL of sim messages)
  analysis.json   – run_analyzer output ({} if analysis didn't run)
  agent_logs.json – dev-agent per-step stdout (test matches only)
  exec_times.json – per-snake per-step CPU times in ms
  budgets.json    – CPU budget config (seconds) that was in force for this run

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
    """All five files in a bundle, parsed.

    Fields are None when the corresponding file was absent (e.g. agent_logs
    is only written for test matches; analysis is empty/absent if the
    analyzer didn't run on a zero-step match).
    """
    replay: list[dict]                            # parsed replay.json JSONL — one sim message per element
    analysis: dict                                # run_analyzer output ({} if it didn't run)
    agent_logs: dict[str, list[str]] | None       # {"0": [step_log_str, ...]}; None for ranked matches
    exec_times: dict[int, list[float]] | None     # seat -> [ms per step]
    budgets: dict[str, float] | None              # everything AgentContainerManager.get_budgets() wrote

    @property
    def budget_ms(self) -> float:
        """Per-step CPU budget in ms, derived from budgets['per_step_seconds'].

        Falls back to 50ms if budgets is missing or malformed — same value the
        scorer used pre-fix, kept here so a corrupt bundle scores something
        rather than crashing.
        """
        if self.budgets is None:
            return 50.0
        per_step_s = self.budgets.get("per_step_seconds")
        return float(per_step_s) * 1000 if per_step_s is not None else 50.0


def _read_optional_json(zf: zipfile.ZipFile, name: str):
    if name not in zf.namelist():
        return None
    return json.loads(zf.read(name))


def read_bundle(bundle_bytes: bytes) -> BundleContents:
    """Parse a bundle zip into all of its component files."""
    with zipfile.ZipFile(io.BytesIO(bundle_bytes)) as zf:
        # replay.json is JSONL, one message per line.
        replay: list[dict] = []
        if "replay.json" in zf.namelist():
            replay_text = zf.read("replay.json").decode()
            replay = [json.loads(line) for line in replay_text.splitlines() if line.strip()]

        analysis = _read_optional_json(zf, "analysis.json") or {}
        agent_logs = _read_optional_json(zf, "agent_logs.json")
        budgets = _read_optional_json(zf, "budgets.json")

        exec_times_raw = _read_optional_json(zf, "exec_times.json")
        exec_times = {int(k): v for k, v in exec_times_raw.items()} if exec_times_raw else None

    return BundleContents(
        replay=replay,
        analysis=analysis,
        agent_logs=agent_logs,
        exec_times=exec_times,
        budgets=budgets,
    )


def assemble_bundle(
    replay_path: Path,
    run_analysis: RunAnalysis | None,
    dev_step_logs: list[str] | None = None,
    exec_times: dict[int, list[float]] | None = None,
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
        if budgets is not None:
            zf.writestr("budgets.json", json.dumps(budgets).encode())
    return buf.getvalue()
