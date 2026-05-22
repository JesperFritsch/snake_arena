# services/orchestrator/orchestrator/redis_observer.py
"""LoopObserver that publishes sim events to a Redis pub/sub channel as JSON.

Live streaming only — no persistence. While the match runs it forwards each
start/step as a {type, data} message (data is the snake_sim model dump).

The terminal frames are deferred: the API closes the stream on the first
"stop" message, and the dev-agent logs only exist once the match has ended
(collected from the container). So this observer records final_step in
notify_stop but does NOT publish it; the orchestrator calls publish_logs()
then publish_stop() once it has the logs, guaranteeing "logs" arrives before
"stop".

Called from the SocketObservable background thread during a test match, so
the Redis client must be the sync variant.
"""
from __future__ import annotations

import json
import logging

import redis

from snake_sim.environment.interfaces.loop_observer_interface import ILoopObserver
from snake_sim.environment.types import LoopStartData, LoopStepData, LoopStopData

log = logging.getLogger(__name__)


class RedisStreamObserver(ILoopObserver):
    def __init__(self, redis_client: redis.Redis, channel: str) -> None:
        self._redis = redis_client
        self._channel = channel
        self.final_step: int | None = None

    def notify_start(self, start_data: LoopStartData) -> None:
        self._publish({"type": "start", "data": start_data.model_dump(mode="json")})

    def notify_step(self, step_data: LoopStepData) -> None:
        self._publish({"type": "step", "data": step_data.model_dump(mode="json")})

    def notify_stop(self, stop_data: LoopStopData) -> None:
        # Defer publishing — orchestrator emits logs then stop, in that order.
        self.final_step = stop_data.final_step

    # ---- terminal frames, driven by the orchestrator after the match ----

    def publish_logs(self, dev_step_logs: list[str] | None) -> None:
        self._publish({"type": "logs", "data": {"agent_logs": {"0": dev_step_logs or []}}})

    def publish_stop(self) -> None:
        self._publish({"type": "stop", "data": {"final_step": self.final_step or 0}})

    def _publish(self, msg: dict) -> None:
        js = json.dumps(msg, separators=(",", ":"))
        try:
            self._redis.publish(self._channel, js)
        except Exception:
            log.warning("Redis publish failed on channel %s", self._channel, exc_info=True)
