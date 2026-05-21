# services/orchestrator/orchestrator/redis_observer.py
"""LoopObserver that publishes sim events to a Redis pub/sub channel as JSON.

Called from the SocketObservable background thread during a test match, so
the Redis client must be the sync variant. We also accumulate messages to
write a bundle.zip at the end containing:
  replay.json      – JSON array of all sim messages (start/step/stop)
  agent_logs.json  – per-seat per-step stdout captured from agent containers
  analysis.json    – TODO: run_analyzer output
"""
from __future__ import annotations

import io
import json
import logging
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import redis

from snake_sim.environment.interfaces.loop_observer_interface import ILoopObserver
from snake_sim.environment.types import LoopStartData, LoopStepData, LoopStopData

if TYPE_CHECKING:
    from docker.models.containers import Container

log = logging.getLogger(__name__)

_STEP_SEP = "---STEP_END---\n"
_DEFAULT_STEP_BUDGET = 2_000  # bytes per step chunk before truncation notice


class RedisStreamObserver(ILoopObserver):
    def __init__(
        self,
        redis_client: redis.Redis,
        channel: str,
        bundle_path: Path | None = None,
        step_stdout_budget_bytes: int = _DEFAULT_STEP_BUDGET,
    ) -> None:
        self._redis = redis_client
        self._channel = channel
        self._bundle_path = bundle_path
        self._step_stdout_budget_bytes = step_stdout_budget_bytes
        self._messages: list[str] = []
        # Populated by set_agent_containers() once containers are started.
        self._seat_containers: dict[int, "Container"] = {}

    def set_agent_containers(self, seat_to_container: dict[int, "Container"]) -> None:
        self._seat_containers = seat_to_container

    # ------------------------------------------------------------------ notify

    def notify_start(self, start_data: LoopStartData) -> None:
        self._publish({"type": "start", "data": _serialize_start(start_data)})

    def notify_step(self, step_data: LoopStepData) -> None:
        self._publish({"type": "step", "data": _serialize_step(step_data)})

    def notify_stop(self, stop_data: LoopStopData) -> None:
        # Collect per-step agent logs while containers are still alive, then
        # emit a single "logs" message before "stop" so the browser receives
        # them before it closes the WebSocket.
        agent_logs = self._collect_agent_logs()
        if agent_logs:
            self._publish({"type": "logs", "data": {"agent_logs": agent_logs}})
        self._publish({"type": "stop", "data": {"final_step": stop_data.final_step}})
        if self._bundle_path:
            self._save_bundle(agent_logs)

    # ------------------------------------------------------------------ helpers

    def _publish(self, msg: dict) -> None:
        js = json.dumps(msg, separators=(",", ":"))
        self._messages.append(js)
        try:
            self._redis.publish(self._channel, js)
        except Exception:
            log.warning("Redis publish failed on channel %s", self._channel, exc_info=True)

    def _collect_agent_logs(self) -> dict[str, list[str]]:
        """Read stdout from the dev agent (seat 0), split by step separator.

        Only seat 0 is captured — opponents' stdout is irrelevant to the user
        who requested the test match.  Each per-step chunk is individually
        capped at step_stdout_budget_bytes so that a chatty early step cannot
        silently crowd out later steps.

        Returns a dict with a single key "0" whose value is a list of log
        chunks, one per step.
        """
        try:
            raw: bytes = self._seat_containers[0].logs()
            text = raw.decode(errors="replace")
        except Exception as e:
            log.warning("failed to read logs for dev agent (seat 0): %s", e)
            return {"0": []}

        raw_chunks = text.split(_STEP_SEP)
        # split() leaves a trailing empty-ish chunk after the last separator.
        if raw_chunks and not raw_chunks[-1].strip():
            raw_chunks = raw_chunks[:-1]

        budget = self._step_stdout_budget_bytes
        chunks: list[str] = []
        for chunk in raw_chunks:
            if len(chunk.encode()) > budget:
                chunk = chunk.encode()[:budget].decode(errors="replace") + (
                    "\n[stdout truncated — output too long for this step]\n"
                )
            chunks.append(chunk)

        return {"0": chunks}

    def _save_bundle(self, agent_logs: dict[str, list[str]] | None) -> None:
        try:
            self._bundle_path.parent.mkdir(parents=True, exist_ok=True)
            replay_bytes = ("[" + ",".join(self._messages) + "]").encode()

            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("replay.json", replay_bytes)
                zf.writestr("analysis.json", b"{}")
                if agent_logs:
                    zf.writestr("agent_logs.json", json.dumps(agent_logs).encode())
            self._bundle_path.write_bytes(buf.getvalue())
            log.info(
                "saved bundle to %s (%d bytes)",
                self._bundle_path,
                self._bundle_path.stat().st_size,
            )
        except Exception:
            log.warning("failed to save bundle to %s", self._bundle_path, exc_info=True)


# ------------------------------------------------------------------ serializers

def _serialize_start(data: LoopStartData) -> dict:
    md = data.env_meta_data
    base_map = md.base_map
    if isinstance(base_map, (bytes, bytearray)):
        dtype_str = str(md.base_map_dtype) if hasattr(md, "base_map_dtype") else "uint8"
        base_map = np.frombuffer(base_map, dtype=np.dtype(dtype_str)).reshape(md.height, md.width)
    return {
        "height": int(md.height),
        "width": int(md.width),
        "free_value": int(md.free_value),
        "blocked_value": int(md.blocked_value),
        "food_value": int(md.food_value),
        "snake_tags": {str(k): v for k, v in md.snake_tags.items()},
        "snake_values": {
            str(k): {"head_value": int(v["head_value"]), "body_value": int(v["body_value"])}
            for k, v in md.snake_values.items()
        },
        "start_positions": {str(k): [int(v.x), int(v.y)] for k, v in md.start_positions.items()},
        "base_map": base_map.tolist(),
    }


def _serialize_step(data: LoopStepData) -> dict:
    return {
        "step": int(data.step),
        "alive_states": {str(k): bool(v) for k, v in data.alive_states.items()},
        "decisions": {str(k): [int(v.x), int(v.y)] for k, v in data.decisions.items()},
        "tail_directions": {str(k): [int(v.x), int(v.y)] for k, v in data.tail_directions.items()},
        "snake_grew": {str(k): bool(v) for k, v in data.snake_grew.items()},
        "lengths": {str(k): int(v) for k, v in data.lengths.items()},
        "new_food": [[int(c.x), int(c.y)] for c in data.new_food],
        "removed_food": [[int(c.x), int(c.y)] for c in data.removed_food],
    }
