# services/sa_common/sa_common/scoring.py
"""Scoring primitives.

Each agent's per-match score is a magnitude-preserving quality score
multiplied by a CPU-efficiency factor:

    final_m = quality_m * cpu_factor_m
    aggregate = mean over the agent's matches of final_m

`quality_m` is the mean across the canonical categories of
`fraction_of_leader`: the agent's raw value as a fraction of the
population leader. A 40× length gap shows up as a 40× score gap, unlike
a rank-based score that would compress it. The population differs by
kind:

  - multi: participants of the single match.
  - solo : eligible agents in the mode (each represented by its mean
    raw per category).

`cpu_factor` is `1 - CPU_PENALTY * min(avg_cpu_ms / avg_budget_ms, 1)`,
bounded in `[1 - CPU_PENALTY, 1]`. The avg_budget is the *absolute*
reference (the sustained-CPU refill rate the cgroup observer enforces),
so a brainless laggard with near-zero CPU can't squash the factor for
everyone else. CPU is a bounded modifier, not a primary axis, so a fast
snake that doesn't play well still loses on quality.

The per-step peak budget (the cgroup hard cap on any single step) is
derived as `avg_budget_ms * PER_STEP_BUDGET_MULTIPLIER`. The orchestrator
applies the multiplier when handing the runner a per-step value; the
runner itself only knows about per-step.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Hashable, Literal, TypeVar


Direction = Literal["higher", "lower"]
ScoringKind = Literal["multi", "solo"]

K = TypeVar("K", bound=Hashable)


# How much of the quality score CPU can take away at full avg_budget.
# 0.40 means an agent averaging right at the sustained ceiling keeps
# 60% of its quality score; a near-zero-CPU agent keeps ~100%.
CPU_PENALTY: float = 0.40

# Ratio of per-step CPU peak to the sustained average ceiling. The
# orchestrator multiplies a mode's `avg_budget_ms` by this to derive the
# per-step cap it hands to the runner. 5× = "you may spend up to 5×
# your average on a single hard step, the bank refills at the average."
PER_STEP_BUDGET_MULTIPLIER: float = 5.0


@dataclass(frozen=True)
class Category:
    name: str
    direction: Direction


# Canonical quality categories per kind. `avg_cpu_ms` is NOT a category —
# it enters the score as a multiplicative `cpu_factor`, not as one of N
# averaged ranks. Adding a category requires the runner to write its base
# value into match_participants.metrics (or a column) and the category-
# value lookup in db/agent_scores.py to know where to read it.
CANONICAL_MULTI_CATEGORIES: list[Category] = [
    Category("survival_rank",  "lower"),
    Category("trapping_count", "higher"),
]

CANONICAL_SOLO_CATEGORIES: list[Category] = [
    Category("final_length", "higher"),
    Category("steps_alive",  "higher"),
]


def categories_for(kind: ScoringKind) -> list[Category]:
    if kind == "multi":
        return CANONICAL_MULTI_CATEGORIES
    if kind == "solo":
        return CANONICAL_SOLO_CATEGORIES
    raise ValueError(f"unknown scoring kind: {kind!r}")


def fraction_of_leader(
    values: dict[K, float],
    direction: Direction,
) -> dict[K, float]:
    """For each key, the entry's raw value as a fraction of the leader's.

    Higher direction: score = value / max_value. Leader gets 1.0, half-
    the-leader gets 0.5.
    Lower direction: score = min_value / value. Leader gets 1.0, double-
    the-leader gets 0.5.

    Magnitude-preserving (unlike `pairwise_win_rate`): a 40× gap between
    leader and laggard maps to a 40× score gap rather than collapsing to
    "winner vs loser." Raises on empty input.
    """
    if not values:
        raise ValueError("fraction_of_leader requires at least 1 entry")
    if direction == "higher":
        leader = max(values.values())
        if leader <= 0:
            # All-zero (or negative) population: no signal to distribute.
            return {k: 0.0 for k in values}
        return {k: max(0.0, min(1.0, v / leader)) for k, v in values.items()}
    # lower
    leader = min(values.values())
    if leader <= 0:
        raise ValueError(
            f"fraction_of_leader: lower-direction leader must be > 0, got {leader}"
        )
    return {
        k: max(0.0, min(1.0, leader / v)) if v > 0 else 0.0
        for k, v in values.items()
    }


def cpu_factor(avg_cpu_ms: float, avg_budget_ms: float) -> float:
    """Multiplicative CPU-efficiency modifier in `[1 - CPU_PENALTY, 1]`.

    A snake using zero CPU keeps 100% of its quality score; one averaging
    right at `avg_budget_ms` keeps `1 - CPU_PENALTY`. The avg budget is
    the absolute reference (matches the cgroup observer's sustained-CPU
    refill rate) so a brainless near-zero-CPU agent can't compress
    everyone else's factor. Agents that go *over* the sustained ceiling
    get killed by the runner; we still saturate at the budget defensively
    so a partial overage in the metrics doesn't yield a negative factor.
    """
    if avg_budget_ms <= 0:
        return 1.0
    ratio = min(max(avg_cpu_ms, 0.0) / avg_budget_ms, 1.0)
    return 1.0 - CPU_PENALTY * ratio
