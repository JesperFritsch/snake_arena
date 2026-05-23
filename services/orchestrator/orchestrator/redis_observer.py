# services/orchestrator/orchestrator/redis_observer.py
"""LoopObserver that publishes sim events to a Redis pub/sub channel as JSON.

Live streaming only — no persistence. While the match runs it forwards each
start/step as a {type, data} message (data is the snake_sim model dump).
Per-step dev-agent stdout is pushed via publish_step_log() (driven by the
runner's log streamer).

The "stop" frame is deferred: the API closes the stream on it, so the
observer records final_step in notify_stop but does NOT publish it — the
orchestrator calls publish_stop() once the match is fully done.

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
    def __init__(self, redis_client: redis.Redis, channel: str, dev_name: str | None = None) -> None:
        self._redis = redis_client
        self._channel = channel
        # The dev agent's DNS name (e.g. "agent_0"). snake_tags values are the
        # targets ("agent_0:50051"), so we match on the host part. Snake ids are
        # NOT reliable: the sim only numbers agents that connected, so a failed
        # dev would let an opponent take id 0.
        self.dev_name = dev_name
        self.final_step: int | None = None
        self.step_count = 0
        # True once the dev agent makes it into the match start, i.e. it survived
        # construction + init + the startup budget. This is the signal that the
        # dev build is runnable (submittable).
        self.dev_reached_start = False

    def notify_start(self, start_data: LoopStartData) -> None:
        if self.dev_name is not None:
            for tag in start_data.env_meta_data.snake_tags.values():
                if tag.split(":", 1)[0] == self.dev_name:
                    self.dev_reached_start = True
                    break
        self._publish({"type": "start", "data": start_data.model_dump(mode="json")})

    def notify_step(self, step_data: LoopStepData) -> None:
        self.step_count += 1
        self._publish({"type": "step", "data": step_data.model_dump(mode="json")})

    def notify_stop(self, stop_data: LoopStopData) -> None:
        # Defer publishing — orchestrator emits logs then stop, in that order.
        self.final_step = stop_data.final_step

    # ---- terminal frames, driven by the orchestrator after the match ----

    def publish_step_log(self, step: int, text: str) -> None:
        """Live, per-step dev-agent stdout (called from the runner's streamer)."""
        self._publish({"type": "step_log", "data": {"step": step, "log": text}})

    def publish_stop(self) -> None:
        self._publish({"type": "stop", "data": {"final_step": self.final_step or 0}})

    def publish_status(self, status: str) -> None:
        """Job-lifecycle transition (running / success / failure / cancelled).
        The API WS closes on a terminal status — this should be the last frame."""
        self._publish({"type": "status", "data": {"status": status}})

    def publish_build(self, status: str, error: str | None = None) -> None:
        """Dev-image build event. status: started | success | failed."""
        data: dict = {"status": status}
        if error is not None:
            data["error"] = error
        self._publish({"type": "build", "data": data})

    def _publish(self, msg: dict) -> None:
        js = json.dumps(msg, separators=(",", ":"))
        try:
            self._redis.publish(self._channel, js)
        except Exception:
            log.warning("Redis publish failed on channel %s", self._channel, exc_info=True)
