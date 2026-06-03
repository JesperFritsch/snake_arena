# services/sa_common/sa_common/db/agent_scores.py
"""Compute per-agent aggregate scores for a mode, on demand.

Replaces the old `agent_scores` table + scorer daemon. The leaderboard
queries call `compute_mode_scores(conn, mode_id)` and get back the same
shape they used to read from the table. Nothing is cached — the result is
recomputed from `match_participants` every time. At current scale that's
fine; if it stops being fine, the cheapest upgrade is a materialised view
over this same query plan, not a new daemon.

For both kinds the aggregate score is the mean across the agent's matches
of `per_match_quality * per_match_cpu_factor`:

  - quality is `fraction_of_leader` averaged across the canonical quality
    categories (population = within-match peers for multi, eligible
    agents for solo).
  - cpu_factor is `1 - CPU_PENALTY * min(avg_cpu_ms / avg_budget_ms, 1)`.

Eligibility: aggregate `score` is None when the agent has played fewer
than `ceil(target_matches_per_version / 2)` matches. Leaderboard reads
exclude None scores; the breakdown is still populated so the inspect view
can show partial progress.

The `category_breakdown` carries per-quality-category mean raw + mean
fraction-of-leader, plus a synthetic `cpu_factor` entry whose `raw` is
the mean `avg_cpu_ms` and `rank` is the mean cpu_factor. The aggregate
score is *not* a simple mean of those breakdown ranks — it's computed
per match so a slow match is penalized on its own quality, not the
agent's overall quality.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row

from sa_common.scoring import (
    Category,
    ScoringKind,
    categories_for,
    cpu_factor,
    fraction_of_leader,
)


@dataclass(slots=True)
class AgentScoreRow:
    project_id: int
    mode_id: int
    project_version: int
    matches_played: int
    score: float | None
    category_breakdown: dict[str, Any]  # {<cat>: {"raw": mean, "rank"?: score}}


@dataclass(slots=True)
class _Participation:
    """One match a project played in this mode. `peers` is the per-category
    raw value for every participant of the same match (including self),
    keyed by seat — used for the multi-mode within-match ranking. Solo
    matches have peers={self.seat: ...} and never use it."""
    match_id: int
    seat: int
    project_id: int
    project_version: int
    category_values: dict[str, float]    # this project's raw values, by category name
    peers: dict[int, dict[str, float]]   # seat -> raw values, by category name
    avg_cpu_ms: float                    # this project's avg_cpu_ms in this match


def _min_matches(target: int) -> int:
    """Eligibility threshold: must have played at least this many ranked
    success matches at the current submitted version to get a non-None
    aggregate score."""
    return max(1, math.ceil(target / 2))


def _category_value(p_row: dict[str, Any], cat: Category) -> float:
    """Look up one quality-category raw value for a participant row dict.

    `survival_rank` and `final_length` live as native columns on
    match_participants; everything else is in the `metrics` JSONB blob.
    `avg_cpu_ms` is handled separately (it's a modifier, not a category).
    """
    if cat.name == "survival_rank":
        return float(p_row["survival_rank"])
    if cat.name == "final_length":
        return float(p_row["final_length"])
    metrics = p_row["metrics"]
    if cat.name not in metrics:
        raise RuntimeError(
            f"participant match_id={p_row['match_id']} seat={p_row['seat']} "
            f"missing category {cat.name!r} (runner didn't write it)"
        )
    return float(metrics[cat.name])


def _fetch_mode(
    conn: psycopg.Connection, mode_id: int,
) -> tuple[ScoringKind, int, float]:
    """Returns (scoring_kind, target_matches_per_version, avg_budget_ms)."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT scoring_kind, target_matches_per_version, avg_budget_ms "
            "FROM modes WHERE id = %s",
            (mode_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"mode_id={mode_id} not found")
    return (
        row["scoring_kind"],
        int(row["target_matches_per_version"]),
        float(row["avg_budget_ms"]),
    )


def _fetch_participations(
    conn: psycopg.Connection,
    mode_id: int,
    categories: list[Category],
) -> list[_Participation]:
    """Pull every participant row for the mode at each project's latest
    submitted version, grouped per match so within-match peers are
    available for multi scoring.

    Filtered to status='success' and is_test=FALSE. No `scored_at` gate —
    every successful ranked match counts the moment the runner records it.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT
                mp.match_id, mp.seat, mp.project_id, mp.project_version,
                mp.final_length, mp.survival_rank, mp.metrics
            FROM match_participants mp
            JOIN matches  m ON m.id = mp.match_id
            JOIN projects p ON p.id = mp.project_id
            WHERE m.mode_id = %s
              AND m.status  = 'success'
              AND m.is_test = FALSE
              AND mp.project_version = p.submitted_version
              AND p.submitted_version > 0
            ORDER BY mp.match_id, mp.seat
            """,
            (mode_id,),
        )
        rows = cur.fetchall()

    # Group rows by match so each participant carries its peers.
    rows_by_match: dict[int, list[dict[str, Any]]] = {}
    for r in rows:
        rows_by_match.setdefault(r["match_id"], []).append(r)

    out: list[_Participation] = []
    for match_id, match_rows in rows_by_match.items():
        peers: dict[int, dict[str, float]] = {
            r["seat"]: {c.name: _category_value(r, c) for c in categories}
            for r in match_rows
        }
        for r in match_rows:
            metrics = r["metrics"]
            if "avg_cpu_ms" not in metrics:
                raise RuntimeError(
                    f"participant match_id={r['match_id']} seat={r['seat']} "
                    f"missing 'avg_cpu_ms' in metrics"
                )
            out.append(_Participation(
                match_id=match_id,
                seat=r["seat"],
                project_id=r["project_id"],
                project_version=r["project_version"],
                category_values=peers[r["seat"]],
                peers=peers,
                avg_cpu_ms=float(metrics["avg_cpu_ms"]),
            ))
    return out


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def compute_mode_scores(
    conn: psycopg.Connection, mode_id: int,
) -> list[AgentScoreRow]:
    """Aggregate scores for every project that has played in this mode.

    Returns one row per project (at its latest submitted version) that has
    played at least one match. Rows below the eligibility threshold are
    still returned, just with `score = None` — the leaderboard layer
    decides whether to include them.
    """
    kind, target_matches, avg_budget_ms = _fetch_mode(conn, mode_id)
    categories = categories_for(kind)
    min_required = _min_matches(target_matches)

    participations = _fetch_participations(conn, mode_id, categories)
    if not participations:
        return []

    # Index by project, picking up the (single) project_version each project
    # appears at — the SQL filter restricts us to one version per project.
    by_project: dict[int, list[_Participation]] = {}
    version_by_project: dict[int, int] = {}
    for p in participations:
        by_project.setdefault(p.project_id, []).append(p)
        version_by_project[p.project_id] = p.project_version

    if kind == "multi":
        return _aggregate_multi(
            mode_id, categories, by_project, version_by_project, min_required, avg_budget_ms,
        )
    if kind == "solo":
        return _aggregate_solo(
            mode_id, categories, by_project, version_by_project, min_required, avg_budget_ms,
        )
    raise RuntimeError(f"unknown scoring kind: {kind!r}")


def _aggregate_multi(
    mode_id: int,
    categories: list[Category],
    by_project: dict[int, list[_Participation]],
    version_by_project: dict[int, int],
    min_required: int,
    avg_budget_ms: float,
) -> list[AgentScoreRow]:
    """Multi aggregate. For each match: per-category fraction-of-leader
    within the match's participants, mean across categories = per-match
    quality. Multiplied by per-match cpu_factor → per-match final.
    Aggregate score = mean of per-match finals."""
    out: list[AgentScoreRow] = []
    for project_id, parts in by_project.items():
        raw_means_acc: dict[str, list[float]] = {c.name: [] for c in categories}
        per_cat_quality_acc: dict[str, list[float]] = {c.name: [] for c in categories}
        per_match_finals: list[float] = []
        per_match_cpu_factors: list[float] = []
        cpu_ms_acc: list[float] = []

        for p in parts:
            if len(p.peers) < 2:
                # 1-snake "multi" match — shouldn't exist (the matchmaker
                # won't emit one); treat as a hard error rather than
                # silently skip.
                raise RuntimeError(
                    f"multi match_id={p.match_id} has {len(p.peers)} participant(s) — "
                    f"multi modes require >= 2"
                )
            per_cat_qs: list[float] = []
            for cat in categories:
                pop = {seat: peer[cat.name] for seat, peer in p.peers.items()}
                fracs = fraction_of_leader(pop, cat.direction)
                own = fracs[p.seat]
                per_cat_qs.append(own)
                per_cat_quality_acc[cat.name].append(own)
                raw_means_acc[cat.name].append(p.category_values[cat.name])
            match_quality = sum(per_cat_qs) / len(per_cat_qs)
            match_cpu = cpu_factor(p.avg_cpu_ms, avg_budget_ms)
            per_match_finals.append(match_quality * match_cpu)
            per_match_cpu_factors.append(match_cpu)
            cpu_ms_acc.append(p.avg_cpu_ms)

        matches_played = len(parts)
        score = _mean(per_match_finals) if matches_played >= min_required else None
        breakdown: dict[str, Any] = {
            c.name: {
                "raw":  _mean(raw_means_acc[c.name]),
                "rank": _mean(per_cat_quality_acc[c.name]),
            }
            for c in categories
        }
        breakdown["cpu_factor"] = {
            "raw":  _mean(cpu_ms_acc),
            "rank": _mean(per_match_cpu_factors),
        }
        out.append(AgentScoreRow(
            project_id=project_id,
            mode_id=mode_id,
            project_version=version_by_project[project_id],
            matches_played=matches_played,
            score=score,
            category_breakdown=breakdown,
        ))
    return out


def _aggregate_solo(
    mode_id: int,
    categories: list[Category],
    by_project: dict[int, list[_Participation]],
    version_by_project: dict[int, int],
    min_required: int,
    avg_budget_ms: float,
) -> list[AgentScoreRow]:
    """Solo aggregate. Each agent's mean raw per quality category sets
    the population basis (leader = best mean). Per match, the agent's raw
    is taken as a fraction of that basis, averaged across categories =
    per-match quality. Multiplied by per-match cpu_factor → per-match
    final. Aggregate score = mean of per-match finals.

    Unqualified projects (matches_played < min_required) excluded from
    the eligible pool — they get `score=None` but the breakdown carries
    raw means + cpu_factor so the inspect view can show progress.
    """
    # Per-project mean raw per quality category (drives the population basis).
    per_project_means: dict[int, dict[str, float]] = {}
    matches_played: dict[int, int] = {}
    for project_id, parts in by_project.items():
        matches_played[project_id] = len(parts)
        means: dict[str, float] = {}
        for cat in categories:
            means[cat.name] = _mean([p.category_values[cat.name] for p in parts])
        per_project_means[project_id] = means

    eligible = [pid for pid, n in matches_played.items() if n >= min_required]

    # Leader basis per category, computed over the eligible population
    # only — keeps an unqualified outlier from warping ranks for the rest.
    leader_basis: dict[str, float] = {}
    if eligible:
        for cat in categories:
            vals = [per_project_means[pid][cat.name] for pid in eligible]
            leader_basis[cat.name] = max(vals) if cat.direction == "higher" else min(vals)

    out: list[AgentScoreRow] = []
    for project_id, parts in by_project.items():
        is_eligible = project_id in eligible
        per_cat_means = per_project_means[project_id]

        # CPU breakdown is always populated (even for unqualified rows so
        # progress is visible).
        per_match_cpus = [cpu_factor(p.avg_cpu_ms, avg_budget_ms) for p in parts]
        avg_cpu_mean = _mean([p.avg_cpu_ms for p in parts])
        cpu_factor_mean = _mean(per_match_cpus)

        breakdown: dict[str, Any] = {}
        per_cat_quality_acc: dict[str, list[float]] = {c.name: [] for c in categories}
        per_match_finals: list[float] = []

        if is_eligible and len(eligible) >= 2:
            for p, p_cpu in zip(parts, per_match_cpus):
                per_cat_qs: list[float] = []
                for cat in categories:
                    raw = p.category_values[cat.name]
                    basis = leader_basis[cat.name]
                    if cat.direction == "higher":
                        frac = 0.0 if basis <= 0 else max(0.0, min(1.0, raw / basis))
                    else:
                        frac = 0.0 if raw <= 0 else max(0.0, min(1.0, basis / raw))
                    per_cat_qs.append(frac)
                    per_cat_quality_acc[cat.name].append(frac)
                match_quality = sum(per_cat_qs) / len(per_cat_qs)
                per_match_finals.append(match_quality * p_cpu)
            score: float | None = _mean(per_match_finals)
        elif is_eligible:
            # Lone qualified agent — quality is degenerate (it's the
            # leader against itself, so 1.0 by definition). CPU still
            # applies; score = mean cpu_factor. Drops to a real value as
            # soon as a second agent qualifies.
            for cat in categories:
                per_cat_quality_acc[cat.name] = [1.0] * len(parts)
            score = cpu_factor_mean
        else:
            score = None

        for cat in categories:
            entry: dict[str, Any] = {"raw": per_cat_means[cat.name]}
            if is_eligible:
                entry["rank"] = _mean(per_cat_quality_acc[cat.name])
            breakdown[cat.name] = entry
        breakdown["cpu_factor"] = {"raw": avg_cpu_mean, "rank": cpu_factor_mean}

        out.append(AgentScoreRow(
            project_id=project_id,
            mode_id=mode_id,
            project_version=version_by_project[project_id],
            matches_played=matches_played[project_id],
            score=score,
            category_breakdown=breakdown,
        ))
    return out


def get_agent_score(
    conn: psycopg.Connection,
    project_id: int,
    mode_id: int,
) -> AgentScoreRow | None:
    """Single-project view. Computes the whole mode and picks the one row —
    cheap enough at current scale; if it stops being cheap, this is the
    place to add a per-project fast path."""
    rows = compute_mode_scores(conn, mode_id)
    for r in rows:
        if r.project_id == project_id:
            return r
    return None
