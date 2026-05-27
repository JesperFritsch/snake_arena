# services/orchestrator/orchestrator/bundle.py
"""Assemble a match bundle (zip) from the pieces a finished match produces.

Shared by the ranked and test-match daemons. The bytes are then stored via an
IBundler. Contents:
  replay.json     – the recorded run (JSONL of sim messages)
  analysis.json   – run_analyzer output ({} if analysis didn't run)
  agent_logs.json – dev-agent per-step stdout (test matches only; omitted when
                    there's no dev agent, e.g. ranked matches)
  exec_times.json – per-snake per-step CPU times in ms (test matches only)
  budgets.json    – CPU budget config (seconds) that was in force for this run
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from snake_sim.analyze.scripts.run_analyzer import RunAnalysis


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
