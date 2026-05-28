# services/sa_common/sa_common/scoring.py
"""Per-participant score computation.

Entry point: compute_scores(exec_times, budget_ms, participants, config)

One parametric formula covers solo and multiplayer modes:

    score = length
          x (1 + beta x length / max(steps_alive, 1))     [eating-rate bonus]
          x (1 + alpha x (1 - (rank - 1) / (n - 1)))      [survival bonus]
          x (budget_ms / max(avg_step_ms, floor_ms)) ^ w  [speed bonus]

  - length is the base — you only grow by eating.
  - The eating-rate bonus rewards efficient eaters over slow grinders. A snake
    at length 30 in 100 steps beats one at length 30 in 500 steps.
  - The survival bonus rewards outlasting opponents in multiplayer. In solo
    modes alpha is 0, collapsing the survival factor to 1.
  - The speed bonus rewards CPU efficiency. floor_ms clamps avg_step_ms so a
    trivial constant-move agent can't game the multiplier.

For test matches (no mode), the scorer applies DEFAULT_CONFIG so users still
get an informational score. See docs/09_ranking_system.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sa_common.types import ParticipantRow


@dataclass(frozen=True)
class ScoringConfig:
    alpha: float = 0.5      # survival weight (multi); 0 in solo modes
    beta: float = 2.0       # eating-rate weight
    w: float = 0.3          # speed multiplier exponent
    floor_ms: float = 2.0   # min avg_step_ms; clamps the speed bonus

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "ScoringConfig":
        """Build from modes.scoring_config JSONB, falling back on defaults."""
        if not d:
            return cls()
        return cls(
            alpha    = float(d.get("alpha",    cls.alpha)),
            beta     = float(d.get("beta",     cls.beta)),
            w        = float(d.get("w",        cls.w)),
            floor_ms = float(d.get("floor_ms", cls.floor_ms)),
        )


# Used for test matches (mode_id IS NULL). Informational; never written to a
# ranked leaderboard. Same shape as a typical multi config.
DEFAULT_CONFIG = ScoringConfig(alpha=0.5, beta=2.0, w=0.3, floor_ms=2.0)


@dataclass(slots=True)
class ParticipantScore:
    seat: int
    project_id: int
    score: float
    avg_step_ms: float
    min_step_ms: float
    max_step_ms: float
    steps_alive: int
    food_rate: float           # length / max(steps_alive, 1)
    eating_factor: float       # 1 + beta * food_rate
    survival_factor: float     # 1 + alpha * (1 - (rank-1)/(n-1))
    speed_multiplier: float    # (budget_ms / max(avg, floor)) ^ w

    def to_metrics(self) -> dict[str, Any]:
        return {
            "score":            round(self.score, 4),
            "avg_step_ms":      round(self.avg_step_ms, 3),
            "min_step_ms":      round(self.min_step_ms, 3),
            "max_step_ms":      round(self.max_step_ms, 3),
            "steps_alive":      self.steps_alive,
            "food_rate":        round(self.food_rate, 4),
            "eating_factor":    round(self.eating_factor, 4),
            "survival_factor": round(self.survival_factor, 4),
            "speed_multiplier": round(self.speed_multiplier, 4),
        }


def compute_scores(
    exec_times: dict[int, list[float]] | None,
    budget_ms: float,
    participants: list[ParticipantRow],
    config: ScoringConfig = DEFAULT_CONFIG,
) -> list[ParticipantScore]:
    """Compute per-participant scores.

    exec_times: {seat: [ms_per_step, ...]} — None if timing data is unavailable,
                in which case speed_multiplier defaults to 1.0 and steps_alive
                falls back to fatal_step (or 0 if also missing).
    budget_ms:  the per-step CPU budget in milliseconds.
    participants: rows from match_participants for this match.
    """
    n = len(participants)
    out: list[ParticipantScore] = []

    for p in participants:
        length = float(p.final_length) if p.final_length is not None else 0.0

        step_times = exec_times.get(p.seat) if exec_times else None
        if step_times:
            steps_alive = len(step_times)
            avg_ms = sum(step_times) / steps_alive
            min_ms = min(step_times)
            max_ms = max(step_times)
        else:
            # No timing data — neutral speed bonus; fall back to fatal_step.
            steps_alive = p.fatal_step if p.fatal_step is not None else 0
            avg_ms = budget_ms
            min_ms = budget_ms
            max_ms = budget_ms

        food_rate     = length / max(steps_alive, 1)
        eating_factor = 1.0 + config.beta * food_rate

        if n <= 1 or p.survival_rank is None:
            survival_factor = 1.0
        else:
            survival_factor = 1.0 + config.alpha * (1.0 - (p.survival_rank - 1) / (n - 1))

        speed_mult = (budget_ms / max(avg_ms, config.floor_ms)) ** config.w

        score = length * eating_factor * survival_factor * speed_mult

        out.append(ParticipantScore(
            seat=p.seat,
            project_id=p.project_id,
            score=score,
            avg_step_ms=avg_ms,
            min_step_ms=min_ms,
            max_step_ms=max_ms,
            steps_alive=steps_alive,
            food_rate=food_rate,
            eating_factor=eating_factor,
            survival_factor=survival_factor,
            speed_multiplier=speed_mult,
        ))

    return out
