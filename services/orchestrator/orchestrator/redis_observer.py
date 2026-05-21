# services/orchestrator/orchestrator/redis_observer.py
"""LoopObserver that publishes sim events to a Redis pub/sub channel as JSON.

Called from the SocketObservable background thread during a test match, so
the Redis client must be the sync variant. We also accumulate messages to
write a .json.gz replay file when the match stops.
"""
from __future__ import annotations

import gzip
import json
import logging
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
        replay_path: Path | None = None,
    ) -> None:
        self._redis = redis_client
        self._channel = channel
        self._replay_path = replay_path
        self._messages: list[str] = []

    # ------------------------------------------------------------------ notify

    def notify_start(self, start_data: LoopStartData) -> None:
        self._publish({"type": "start", "data": _serialize_start(start_data)})

    def notify_step(self, step_data: LoopStepData) -> None:
        self._publish({"type": "step", "data": _serialize_step(step_data)})

    def notify_stop(self, stop_data: LoopStopData) -> None:
        self._publish({"type": "stop", "data": {"final_step": stop_data.final_step}})
        if self._replay_path:
            self._save_replay()

    # ------------------------------------------------------------------ helpers

    def _publish(self, msg: dict) -> None:
        js = json.dumps(msg, separators=(",", ":"))
        self._messages.append(js)
        try:
            self._redis.publish(self._channel, js)
        except Exception:
            log.warning("Redis publish failed on channel %s", self._channel, exc_info=True)

    def _save_replay(self) -> None:
        try:
            self._replay_path.parent.mkdir(parents=True, exist_ok=True)
            payload = ("[" + ",".join(self._messages) + "]").encode()
            with gzip.open(self._replay_path, "wb") as f:
                f.write(payload)
            log.info("saved replay to %s (%d bytes compressed)", self._replay_path, self._replay_path.stat().st_size)
        except Exception:
            log.warning("failed to save replay to %s", self._replay_path, exc_info=True)


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
