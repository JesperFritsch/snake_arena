# services/orchestrator/orchestrator/redis_observer.py
"""LoopObserver that publishes sim events to a Redis pub/sub channel as JSON.

Called from the SocketObservable background thread during a test match, so
the Redis client must be the sync variant. We also accumulate messages to
write a bundle.zip at the end containing:
  replay.json   – JSON array of all sim messages
  analysis.json – TODO: run_analyzer output (empty for now)
  run.log       – TODO: build/run log output (empty for now)
"""
from __future__ import annotations

import io
import json
import logging
import zipfile
from pathlib import Path

import numpy as np
import redis

from snake_sim.environment.interfaces.loop_observer_interface import ILoopObserver
from snake_sim.environment.types import LoopStartData, LoopStepData, LoopStopData

log = logging.getLogger(__name__)


class RedisStreamObserver(ILoopObserver):
    def __init__(
        self,
        redis_client: redis.Redis,
        channel: str,
        bundle_path: Path | None = None,
    ) -> None:
        self._redis = redis_client
        self._channel = channel
        self._bundle_path = bundle_path
        self._messages: list[str] = []

    # ------------------------------------------------------------------ notify

    def notify_start(self, start_data: LoopStartData) -> None:
        self._publish({"type": "start", "data": _serialize_start(start_data)})

    def notify_step(self, step_data: LoopStepData) -> None:
        self._publish({"type": "step", "data": _serialize_step(step_data)})

    def notify_stop(self, stop_data: LoopStopData) -> None:
        self._publish({"type": "stop", "data": {"final_step": stop_data.final_step}})
        if self._bundle_path:
            self._save_bundle()

    # ------------------------------------------------------------------ helpers

    def _publish(self, msg: dict) -> None:
        js = json.dumps(msg, separators=(",", ":"))
        self._messages.append(js)
        try:
            self._redis.publish(self._channel, js)
        except Exception:
            log.warning("Redis publish failed on channel %s", self._channel, exc_info=True)

    def _save_bundle(self) -> None:
        try:
            self._bundle_path.parent.mkdir(parents=True, exist_ok=True)
            replay_bytes = ("[" + ",".join(self._messages) + "]").encode()

            # TODO: populate analysis.json with run_analyzer output
            analysis_bytes = b"{}"

            # TODO: populate run.log with build/run logs captured during the match
            run_log_bytes = b""

            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("replay.json",   replay_bytes)
                zf.writestr("analysis.json", analysis_bytes)
                zf.writestr("run.log",       run_log_bytes)
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
