# services/orchestrator/orchestrator/replay.py
"""Utilities for reading raw replay files produced by the sim."""
from __future__ import annotations

from pathlib import Path

from snake_sim.environment.types import NoMoreSteps
from snake_sim.loop_observables.file_reader_observable import FileRepeaterObservable
from snake_sim.loop_observers.state_builder_observer import StateBuilderObserver
from snake_sim.loop_observers.waitable_observer import WaitableObserver


def extract_final_lengths(replay_path: Path) -> dict[int, int]:
    """Return {snake_id: final_body_length} by replaying to the last frame."""
    loop_repeater = FileRepeaterObservable(filepath=replay_path)
    state_builder = StateBuilderObserver()
    waitable = WaitableObserver()
    loop_repeater.add_observer(state_builder)
    loop_repeater.add_observer(waitable)
    loop_repeater.start()
    waitable.wait_until_started()

    state = state_builder.get_state(0)
    while True:
        try:
            state = state_builder.get_next_state()
        except StopIteration:
            break
        except NoMoreSteps:
            continue

    return {sid: len(body) for sid, body in state.snake_bodies.items()}
