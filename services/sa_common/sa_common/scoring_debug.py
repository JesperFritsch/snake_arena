# services/sa_common/sa_common/scoring_debug.py
"""score_match CLI — print full scoring details for one match.

Identify the match by id or uuid (the MatchModal in the UI surfaces both).
For multi modes the per-match score is recomputed from the raw values
across the match's participants. For solo modes the per-match output is
just the raw values (a per-match score isn't defined); the agent's
current aggregate position is fetched live from compute_mode_scores so
you can see which category is sinking it.

Usage:
    score_match <id>
    score_match <uuid>
"""
from __future__ import annotations

import sys

import psycopg

from sa_common.db.agent_scores import get_agent_score
from sa_common.db.connection import get_conn
from sa_common.db.matches import Match, get_match, get_match_by_uuid, get_match_participants
from sa_common.db.modes import Mode, get_mode
from sa_common.db.projects import get_project_meta
from sa_common.scoring import (
    Category,
    PER_STEP_BUDGET_MULTIPLIER,
    ScoringKind,
    categories_for,
    cpu_factor,
    fraction_of_leader,
)
from sa_common.types import ParticipantRow


def _resolve_match(conn: psycopg.Connection, ref: str) -> Match:
    match: Match | None
    if ref.isdigit():
        match = get_match(conn, int(ref))
        label = f"id={ref}"
    else:
        match = get_match_by_uuid(conn, ref)
        label = f"uuid={ref}"
    if match is None:
        raise SystemExit(f"match not found ({label})")
    return match


def _category_value(p: ParticipantRow, cat: Category) -> float:
    if cat.name == "survival_rank":
        return float(p.survival_rank)
    if cat.name == "final_length":
        return float(p.final_length)
    return float(p.metrics[cat.name])


def _fmt_kv(rows: list[tuple[str, str]]) -> str:
    """Aligned `key = value` lines."""
    if not rows:
        return ""
    width = max(len(k) for k, _ in rows)
    return "\n".join(f"    {k:<{width}} = {v}" for k, v in rows)


def _fmt_float(v: float | None, digits: int = 3) -> str:
    if v is None:
        return "—"
    return f"{v:.{digits}f}"


def _print_multi(
    match: Match,
    mode: Mode,
    categories: list[Category],
    participants_with_names: list[tuple[ParticipantRow, str, str]],
) -> None:
    rows = [p for p, _, _ in participants_with_names]
    n = len(rows)
    if n < 2:
        print(f"\n(multi match with {n} participant(s) — cannot rank)")
        return

    # Per-category fraction_of_leader within the match.
    cat_scores_by_seat: dict[int, dict[str, float]] = {p.seat: {} for p in rows}
    raw_by_seat: dict[int, dict[str, float]] = {p.seat: {} for p in rows}
    for cat in categories:
        pop = {p.seat: _category_value(p, cat) for p in rows}
        fracs = fraction_of_leader(pop, cat.direction)
        for seat, frac in fracs.items():
            cat_scores_by_seat[seat][cat.name] = frac
            raw_by_seat[seat][cat.name] = pop[seat]

    print(f"\nMULTI mode — recomputing per-match score from raw values")
    print(f"  formula: quality = mean fraction_of_leader across "
          f"{len(categories)} categories (N = {n});  "
          f"final = quality * cpu_factor(avg_cpu_ms, avg_budget={mode.avg_budget_ms} ms)")

    for p, name, lang in participants_with_names:
        print()
        print(f"  seat={p.seat}  {name} (v{p.project_version}, {lang})")
        cat_rows: list[tuple[str, str]] = []
        per_cat = cat_scores_by_seat[p.seat]
        for cat in categories:
            raw = raw_by_seat[p.seat][cat.name]
            score = per_cat[cat.name]
            arrow = "↑" if cat.direction == "higher" else "↓"
            cat_rows.append((
                f"{cat.name} {arrow}",
                f"raw={_fmt_float(raw)}  frac={_fmt_float(score, 3)}",
            ))
        quality = sum(per_cat.values()) / len(per_cat)
        avg_cpu = float(p.metrics["avg_cpu_ms"])
        cpu_f = cpu_factor(avg_cpu, mode.avg_budget_ms)
        final = quality * cpu_f
        print(_fmt_kv(cat_rows))
        print(_fmt_kv([
            ("quality",    _fmt_float(quality, 4)),
            ("avg_cpu_ms", f"{_fmt_float(avg_cpu)}  (cpu_factor={_fmt_float(cpu_f, 3)})"),
            ("final",      _fmt_float(final, 4)),
        ]))


def _print_solo(
    conn: psycopg.Connection,
    match: Match,
    mode: Mode,
    categories: list[Category],
    participants_with_names: list[tuple[ParticipantRow, str, str]],
) -> None:
    print(f"\nSOLO mode — per-match score depends on the population basis; "
          f"showing raw values + per-match cpu_factor + agent aggregate "
          f"position (computed live)")

    for p, name, lang in participants_with_names:
        print()
        print(f"  seat={p.seat}  {name} (v{p.project_version}, {lang})")
        cat_rows: list[tuple[str, str]] = []
        for cat in categories:
            raw = _category_value(p, cat)
            arrow = "↑" if cat.direction == "higher" else "↓"
            cat_rows.append((f"{cat.name} {arrow}", f"raw={_fmt_float(raw)}"))
        avg_cpu = float(p.metrics["avg_cpu_ms"])
        cpu_f = cpu_factor(avg_cpu, mode.avg_budget_ms)
        cat_rows.append((
            "avg_cpu_ms",
            f"raw={_fmt_float(avg_cpu)}  (cpu_factor={_fmt_float(cpu_f, 3)})",
        ))
        print("  per-match raw values:")
        print(_fmt_kv(cat_rows))

        agg = get_agent_score(conn, p.project_id, mode.id)
        if agg is None:
            print("  aggregate: (no scored matches yet)")
            continue
        print(f"  aggregate (matches_played={agg.matches_played}, "
              f"score={_fmt_float(agg.score, 4)}):")
        agg_rows: list[tuple[str, str]] = []
        for cat in categories:
            entry = agg.category_breakdown.get(cat.name, {})
            raw_mean = entry.get("raw")
            pop_frac = entry.get("rank")
            arrow = "↑" if cat.direction == "higher" else "↓"
            agg_rows.append((
                f"{cat.name} {arrow}",
                f"mean={_fmt_float(raw_mean)}  frac_of_leader={_fmt_float(pop_frac, 3)}",
            ))
        cpu_entry = agg.category_breakdown.get("cpu_factor", {})
        agg_rows.append((
            "cpu_factor",
            f"mean_cpu_ms={_fmt_float(cpu_entry.get('raw'))}  "
            f"mean_factor={_fmt_float(cpu_entry.get('rank'), 3)}",
        ))
        print(_fmt_kv(agg_rows))


def _print_match_header(match: Match, mode: Mode | None) -> None:
    print(f"Match {match.id}")
    print(f"  uuid       = {match.match_uuid}")
    print(f"  status     = {match.status}")
    print(f"  is_test    = {match.is_test}")
    print(f"  started_at = {match.started_at:%Y-%m-%d %H:%M:%S}")
    if mode is None:
        print(f"  mode       = (test match — no mode_id)")
    else:
        peak = mode.avg_budget_ms * PER_STEP_BUDGET_MULTIPLIER
        print(f"  mode          = [{mode.id}] {mode.slug}  (kind={mode.scoring_kind})")
        print(f"  avg_budget_ms = {mode.avg_budget_ms}  (per-step peak = {peak:g} ms)")


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] in ("-h", "--help"):
        print("usage: score_match <id|uuid>", file=sys.stderr)
        raise SystemExit(2)

    ref = sys.argv[1]

    with get_conn(autocommit=False) as conn:
        match = _resolve_match(conn, ref)
        mode = get_mode(conn, match.mode_id) if match.mode_id is not None else None

        _print_match_header(match, mode)

        parts = get_match_participants(conn, match.id)
        if not parts:
            print("\n(no participants)")
            return

        named: list[tuple[ParticipantRow, str, str]] = []
        for p in parts:
            meta = get_project_meta(conn, p.project_id)
            name = meta.name if meta else f"project_id={p.project_id}"
            lang = meta.language if meta else "?"
            named.append((p, name, lang))

        if mode is None:
            print("\n(test match — no scoring kind; raw participant values:)")
            for p, name, lang in named:
                print(f"  seat={p.seat}  {name} (v{p.project_version}, {lang})")
                rows = [
                    ("final_length",  str(p.final_length)),
                    ("survival_rank", str(p.survival_rank)),
                ]
                rows.extend((k, str(v)) for k, v in p.metrics.items())
                print(_fmt_kv(rows))
            return

        kind: ScoringKind = mode.scoring_kind
        categories = categories_for(kind)
        if kind == "multi":
            _print_multi(match, mode, categories, named)
        else:
            _print_solo(conn, match, mode, categories, named)


if __name__ == "__main__":
    main()
