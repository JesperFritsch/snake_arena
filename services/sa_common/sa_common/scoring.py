# services/sa_common/sa_common/scoring.py
"""Per-participant score computation.

Entry point: compute_scores(exec_times, budget_ms, participants, config)

One parametric formula covers solo and multiplayer modes:

    score = length
          x (1 + beta x length / max(steps_alive, 1))     [eating-rate bonus]
          x (1 + alpha x (1 - (rank - 1) / (n - 1)))      [survival bonus]
          x (budget_ms / max(avg_step_ms, floor_ms)) ^ w  [speed bonus]

  - length is the base — you only grow by eating.
  - The eating-rate bonus rewards efficient eaters over slow grinders.
  - The survival bonus rewards outlasting opponents in multiplayer. In solo
    modes alpha is 0, collapsing the survival factor to 1.
  - The speed bonus rewards CPU efficiency. floor_ms clamps avg_step_ms so a
    trivial constant-move agent can't game the multiplier.

Inputs are taken as truth: missing exec_times, missing final_length, or
missing survival_rank cause a raise. The scorer catches that, releases the
lease, increments scoring_attempts, and eventually gives up — much better
than silently emitting a score from defaults that would mislead the
leaderboard.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sa_common.types import ParticipantRow


@dataclass(frozen=True)
class ScoringConfig:
    alpha: float       # survival weight (multi); 0 in solo modes
    beta: float        # eating-rate weight
    w: float           # speed multiplier exponent
    floor_ms: float    # min avg_step_ms; clamps the speed bonus

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ScoringConfig":
        """Build from modes.scoring_config JSONB. All four keys are required."""
        return cls(
            alpha    = float(d["alpha"]),
            beta     = float(d["beta"]),
            w        = float(d["w"]),
            floor_ms = float(d["floor_ms"]),
        )


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
    exec_times: dict[int, list[float]],
    budget_ms: float,
    participants: list[ParticipantRow],
    config: ScoringConfig,
) -> list[ParticipantScore]:
    """Compute per-participant scores. Every input is required.

    exec_times    must contain a non-empty list for every participant's seat.
    final_length  must be set on every participant.
    survival_rank must be set on every participant in multi modes (alpha > 0).
    """
    n = len(participants)
    out: list[ParticipantScore] = []

    for p in participants:
        if p.final_length is None:
            raise ValueError(f"seat={p.seat} has no final_length")
        if p.seat not in exec_times:
            raise ValueError(f"seat={p.seat} has no exec_times entry")
        step_times = exec_times[p.seat]
        if not step_times:
            raise ValueError(f"seat={p.seat} has empty exec_times list")
        if config.alpha > 0 and p.survival_rank is None:
            raise ValueError(
                f"seat={p.seat}: survival_rank is required when alpha > 0"
            )

        length = float(p.final_length)
        steps_alive = len(step_times)
        avg_ms = sum(step_times) / steps_alive
        min_ms = min(step_times)
        max_ms = max(step_times)

        food_rate     = length / steps_alive
        eating_factor = 1.0 + config.beta * food_rate

        if config.alpha == 0 or n == 1:
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
