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
        # Runner-controlled seat -> tag mapping (e.g. {0: "agent_0:50051"}).
        # Set after resolve_*_agents so notify_start can publish the join with
        # the sim's snake_id -> tag map. None disables the join (frontend will
        # fall back to assuming seat == snake_id, which is brittle — set this).
        self.target_by_seat: dict[int, str] | None = None
        self.final_step: int | None = None
        self.step_count = 0
        # True once the dev agent makes it into the match start, i.e. it survived
        # construction + init + the startup budget. This is the signal that the
        # dev build is runnable (submittable).
        self.dev_reached_start = False

    def notify_start(self, start_data: LoopStartData) -> None:
        snake_tags = start_data.env_meta_data.snake_tags
        if self.dev_name is not None:
            for tag in snake_tags.values():
                if tag.split(":", 1)[0] == self.dev_name:
                    self.dev_reached_start = True
                    break
        data = start_data.model_dump(mode="json")
        # Publish the sim<->runner identifier join so the frontend can color
        # snakes and label exec_times consistently. snake_id -> seat: the
        # frontend uses seat as its stable identity (it indexes
        # participant_names / exec_times); the renderer needs this map to
        # paint each snake_id in the right seat's color.
        if self.target_by_seat is not None:
            seat_by_target = {t: s for s, t in self.target_by_seat.items()}
            data["seat_by_snake_id"] = {
                str(snake_id): seat_by_target[tag]
                for snake_id, tag in snake_tags.items()
                if tag in seat_by_target
            }
        self._publish({"type": "start", "data": data})

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

    def publish_exec_time(self, step: int, times: dict[int, float]) -> None:
        """Per-step container CPU time (ms) keyed by seat (matches the
        bundle's exec_times.json and the frontend's participant_names
        ordering — frontend doesn't have to translate from snake_id)."""
        self._publish({"type": "exec_time", "data": {"step": step, "times": times}})

    def publish_stop(self) -> None:
        # final_step stays None if notify_stop never fired (e.g. the sim
        # aborted before the loop produced any steps). Frontend handles null.
        self._publish({"type": "stop", "data": {"final_step": self.final_step}})

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
