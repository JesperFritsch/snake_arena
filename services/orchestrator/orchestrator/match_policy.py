# services/orchestrator/orchestrator/match_policy.py
"""Match settings shared by the ranked and test runners.

Anything that affects how an agent is judged (CPU budgets, game-ending
rules) lives here so a test match enforces exactly what a ranked match
would. Runner-specific behaviour (dev-seat console, end-when-dev-dies)
stays in the daemons.
"""
from __future__ import annotations

from typing import Any

from sa_common.scoring import per_step_budget_seconds

# Once "alone AND longest" first holds, give the survivor this many extra
# steps to actually grow length. Without the buffer the rule fires the
# moment the survivor edges past the longest dead snake (often just 1 apple
# ahead), so a smart snake gets no time to demonstrate length over
# short-lived opponents.
LAST_STANDING_BUFFER_STEPS = 200


def run_match_kwargs(avg_budget_ms: float, participant_count: int) -> dict[str, Any]:
    """Budget and end-rule kwargs for `runner.match.run_match`."""
    multi = participant_count > 1
    return {
        # Mode owns the sustained-average CPU budget; the per-step peak the
        # runner enforces is derived from it. The bundle captures the actual
        # enforced values.
        "per_step_budget_seconds": per_step_budget_seconds(avg_budget_ms),
        # Multi matches end once one snake is alive AND longest — shortens
        # the long tail without making suicide a winning move (a suicidal
        # snake locks in its length at death). MUST NOT be set for solo:
        # with one snake total, it is trivially both "last standing" and
        # "longest" from step 0, so the rule fires immediately and the
        # match ends without ever moving.
        "end_on_last_standing_when_longest": multi,
        "end_on_last_standing_buffer_steps": LAST_STANDING_BUFFER_STEPS if multi else None,
    }
