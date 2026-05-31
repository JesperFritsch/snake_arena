# services/sa_common/sa_common/db/modes.py
"""DB layer for ranked-match modes.

A mode is one persistent evaluation configuration (participant count, sim
args, scoring weights, target matches per submission version). Seeded in
migrations/001.sql. Read by scheduler, matchmaker, scorer, API.

See docs/09_ranking_system.md.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from sa_common.db.connection import get_conn


@dataclass(slots=True)
class Mode:
    id: int
    slug: str
    name: str
    description: str | None
    group_slug: str | None
    participant_count: int
    sim_args: dict[str, Any]
    map_slug: str | None
    budget_ms: float
    scoring_config: dict[str, Any]
    target_matches_per_version: int
    enabled: bool


_MODE_COLUMNS = """
    id, slug, name, description, group_slug,
    participant_count, sim_args, map_slug,
    budget_ms, scoring_config,
    target_matches_per_version, enabled
"""

_SIM_ARGS_KEYS = ("food", "grid_width", "grid_height", "map")
_SCORING_KEYS = ("alpha", "beta", "w", "floor_ms")


def _row_to_mode(row: dict[str, Any]) -> Mode:
    return Mode(
        id=row["id"],
        slug=row["slug"],
        name=row["name"],
        description=row["description"],
        group_slug=row["group_slug"],
        participant_count=row["participant_count"],
        sim_args=row["sim_args"],
        map_slug=row["map_slug"],
        budget_ms=float(row["budget_ms"]),
        scoring_config=row["scoring_config"],
        target_matches_per_version=row["target_matches_per_version"],
        enabled=row["enabled"],
    )


def list_modes(conn: psycopg.Connection, enabled_only: bool = True) -> list[Mode]:
    where = "WHERE enabled = TRUE" if enabled_only else ""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(f"SELECT {_MODE_COLUMNS} FROM modes {where} ORDER BY id")
        return [_row_to_mode(r) for r in cur.fetchall()]


def get_mode(conn: psycopg.Connection, mode_id: int) -> Mode | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"SELECT {_MODE_COLUMNS} FROM modes WHERE id = %s", (mode_id,),
        )
        row = cur.fetchone()
        return _row_to_mode(row) if row else None


def get_mode_by_slug(conn: psycopg.Connection, slug: str) -> Mode | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"SELECT {_MODE_COLUMNS} FROM modes WHERE slug = %s", (slug,),
        )
        row = cur.fetchone()
        return _row_to_mode(row) if row else None


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------

def set_mode_enabled(
    conn: psycopg.Connection, mode_id: int, enabled: bool
) -> bool:
    """Flip the enabled flag. Returns True if the row existed."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE modes SET enabled = %s WHERE id = %s",
            (enabled, mode_id),
        )
        return cur.rowcount > 0


def update_mode(
    conn: psycopg.Connection,
    mode_id: int,
    *,
    name: str | None = None,
    description: str | None = None,
    participant_count: int | None = None,
    budget_ms: float | None = None,
    target_matches_per_version: int | None = None,
    map_slug: str | None = None,
    clear_map_slug: bool = False,
    group_slug: str | None = None,
    clear_group_slug: bool = False,
    sim_args_updates: dict[str, Any] | None = None,
    scoring_config_updates: dict[str, Any] | None = None,
) -> Mode | None:
    """Partial update — only fields explicitly passed are touched.

    `sim_args_updates` / `scoring_config_updates` merge into the existing
    JSONB blob rather than replacing it. Use `clear_map_slug=True` /
    `clear_group_slug=True` to set those columns to NULL (since `None`
    means "leave alone").
    """
    current = get_mode(conn, mode_id)
    if current is None:
        return None

    sets: list[str] = []
    params: list[Any] = []

    if name is not None:
        sets.append("name = %s")
        params.append(name)
    if description is not None:
        sets.append("description = %s")
        params.append(description)
    if participant_count is not None:
        sets.append("participant_count = %s")
        params.append(participant_count)
    if budget_ms is not None:
        sets.append("budget_ms = %s")
        params.append(budget_ms)
    if target_matches_per_version is not None:
        sets.append("target_matches_per_version = %s")
        params.append(target_matches_per_version)
    if clear_map_slug:
        sets.append("map_slug = NULL")
    elif map_slug is not None:
        sets.append("map_slug = %s")
        params.append(map_slug)
    if clear_group_slug:
        sets.append("group_slug = NULL")
    elif group_slug is not None:
        sets.append("group_slug = %s")
        params.append(group_slug)
    if sim_args_updates:
        merged = {**current.sim_args, **sim_args_updates}
        sets.append("sim_args = %s")
        params.append(Jsonb(merged))
    if scoring_config_updates:
        merged = {**current.scoring_config, **scoring_config_updates}
        sets.append("scoring_config = %s")
        params.append(Jsonb(merged))

    if not sets:
        return current

    params.append(mode_id)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE modes SET {', '.join(sets)} WHERE id = %s",
            params,
        )
    return get_mode(conn, mode_id)


def create_mode(
    conn: psycopg.Connection,
    *,
    slug: str,
    name: str,
    participant_count: int,
    sim_args: dict[str, Any],
    budget_ms: float,
    scoring_config: dict[str, Any],
    target_matches_per_version: int,
    description: str | None = None,
    group_slug: str | None = None,
    map_slug: str | None = None,
    enabled: bool = True,
) -> int:
    """Insert a new mode. Returns the new mode's id."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO modes (
                slug, name, description, group_slug, participant_count, sim_args,
                map_slug, budget_ms, scoring_config,
                target_matches_per_version, enabled
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                slug, name, description, group_slug, participant_count, Jsonb(sim_args),
                map_slug, budget_ms, Jsonb(scoring_config),
                target_matches_per_version, enabled,
            ),
        )
        row = cur.fetchone()
        assert row is not None
        return row[0]


def delete_mode(conn: psycopg.Connection, mode_id: int) -> bool:
    """Delete a mode. Returns True if a row was removed.

    Will raise if any match_jobs reference this mode (FK ON DELETE RESTRICT).
    Disable instead if you just want to stop scheduling matches.
    """
    with conn.cursor() as cur:
        cur.execute("DELETE FROM modes WHERE id = %s", (mode_id,))
        return cur.rowcount > 0


# --------------------------------------------------------------------------
# CLI — for ops and manual tweaking
# --------------------------------------------------------------------------

def _resolve(conn: psycopg.Connection, identifier: str) -> Mode:
    """Look up a mode by numeric id or slug; exit if not found."""
    mode: Mode | None
    if identifier.isdigit():
        mode = get_mode(conn, int(identifier))
    else:
        mode = get_mode_by_slug(conn, identifier)
    if mode is None:
        raise SystemExit(f"mode {identifier!r} not found")
    return mode


def _format_mode_line(m: Mode) -> str:
    flag = "on " if m.enabled else "off"
    group = m.group_slug or "-"
    return (
        f"[{m.id:>3}] {flag}  {m.slug:<24} group={group:<10} "
        f"p={m.participant_count} target={m.target_matches_per_version} "
        f"budget={m.budget_ms}ms  sim_args={json.dumps(m.sim_args)}  "
        f"scoring={json.dumps(m.scoring_config)}"
    )


def _format_mode_detail(m: Mode) -> str:
    return "\n".join([
        f"id:                         {m.id}",
        f"slug:                       {m.slug}",
        f"name:                       {m.name}",
        f"description:                {m.description}",
        f"group_slug:                 {m.group_slug}",
        f"enabled:                    {m.enabled}",
        f"participant_count:          {m.participant_count}",
        f"map_slug:                   {m.map_slug}",
        f"budget_ms:                  {m.budget_ms}",
        f"target_matches_per_version: {m.target_matches_per_version}",
        f"sim_args:                   {json.dumps(m.sim_args)}",
        f"scoring_config:             {json.dumps(m.scoring_config)}",
    ])


def _add_set_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--name")
    p.add_argument("--description")
    p.add_argument("--participant-count", type=int)
    p.add_argument("--budget-ms", type=float)
    p.add_argument("--target-matches-per-version", type=int)
    map_group = p.add_mutually_exclusive_group()
    map_group.add_argument("--map-slug", help="set the modes.map_slug column")
    map_group.add_argument(
        "--clear-map-slug", action="store_true",
        help="set map_slug to NULL (open grid)",
    )
    group_group = p.add_mutually_exclusive_group()
    group_group.add_argument(
        "--group-slug",
        help="assign this mode to a mode_groups.slug (see `db_mode_groups list`)",
    )
    group_group.add_argument(
        "--clear-group-slug", action="store_true",
        help="detach this mode from its group (renders as its own tab)",
    )
    # sim_args fields (merged into the JSONB blob)
    p.add_argument("--food", type=int)
    p.add_argument("--grid-width", type=int)
    p.add_argument("--grid-height", type=int)
    p.add_argument(
        "--map", dest="sim_map",
        help="sim_args.map (snake_sim map id; distinct from --map-slug column)",
    )
    # scoring_config fields
    p.add_argument("--alpha", type=float)
    p.add_argument("--beta", type=float)
    p.add_argument("--w", type=float)
    p.add_argument("--floor-ms", type=float)


def _collect_updates(args: argparse.Namespace) -> dict[str, Any]:
    """Pull --set flag values off args into kwargs for update_mode()."""
    sim_args_updates: dict[str, Any] = {}
    if args.food is not None:
        sim_args_updates["food"] = args.food
    if args.grid_width is not None:
        sim_args_updates["grid_width"] = args.grid_width
    if args.grid_height is not None:
        sim_args_updates["grid_height"] = args.grid_height
    if args.sim_map is not None:
        sim_args_updates["map"] = args.sim_map

    scoring_updates: dict[str, Any] = {}
    if args.alpha is not None:
        scoring_updates["alpha"] = args.alpha
    if args.beta is not None:
        scoring_updates["beta"] = args.beta
    if args.w is not None:
        scoring_updates["w"] = args.w
    if args.floor_ms is not None:
        scoring_updates["floor_ms"] = args.floor_ms

    return {
        "name": args.name,
        "description": args.description,
        "participant_count": args.participant_count,
        "budget_ms": args.budget_ms,
        "target_matches_per_version": args.target_matches_per_version,
        "map_slug": args.map_slug,
        "clear_map_slug": args.clear_map_slug,
        "group_slug": args.group_slug,
        "clear_group_slug": args.clear_group_slug,
        "sim_args_updates": sim_args_updates or None,
        "scoring_config_updates": scoring_updates or None,
    }


def cli(argv) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage ranked-match modes.")
    sub = parser.add_subparsers(dest="command", required=True)

    list_p = sub.add_parser("list", help="list modes")
    list_p.add_argument(
        "--all", action="store_true",
        help="include disabled modes (default: enabled only)",
    )

    get_p = sub.add_parser("get", help="show one mode")
    get_p.add_argument("identifier", help="mode id or slug")

    enable_p = sub.add_parser("enable", help="enable a mode")
    enable_p.add_argument("identifier", help="mode id or slug")

    disable_p = sub.add_parser("disable", help="disable a mode")
    disable_p.add_argument("identifier", help="mode id or slug")

    set_p = sub.add_parser("set", help="update mode fields (only flags you pass are touched)")
    set_p.add_argument("identifier", help="mode id or slug")
    _add_set_flags(set_p)

    create_p = sub.add_parser("create", help="insert a new mode")
    create_p.add_argument("--slug", required=True)
    create_p.add_argument("--name", required=True)
    create_p.add_argument("--participant-count", type=int, required=True)
    create_p.add_argument("--budget-ms", type=float, required=True)
    create_p.add_argument("--target-matches-per-version", type=int, required=True)
    create_p.add_argument("--food", type=int, required=True)
    create_p.add_argument("--grid-width", type=int)
    create_p.add_argument("--grid-height", type=int)
    create_p.add_argument("--map", dest="sim_map", help="sim_args.map (snake_sim map id)")
    create_p.add_argument("--alpha", type=float, default=0.5)
    create_p.add_argument("--beta", type=float, default=2.0)
    create_p.add_argument("--w", type=float, default=0.3)
    create_p.add_argument("--floor-ms", type=float, default=2.0)
    create_p.add_argument("--description")
    create_p.add_argument(
        "--group-slug",
        help="bundle this mode under a mode_groups.slug (e.g. 'solo')",
    )
    create_p.add_argument("--map-slug", help="modes.map_slug column (not sim_args.map)")
    create_p.add_argument(
        "--disabled", action="store_true",
        help="create the mode in disabled state",
    )

    delete_p = sub.add_parser(
        "delete",
        help="delete a mode (will fail if any match_jobs reference it; "
             "use `disable` instead to stop scheduling)",
    )
    delete_p.add_argument("identifier", help="mode id or slug")

    return parser.parse_args(argv)


def main():
    args = cli(sys.argv[1:])
    with get_conn(autocommit=False) as conn:
        with conn.transaction():
            if args.command == "list":
                modes = list_modes(conn, enabled_only=not args.all)
                result = (
                    "\n".join(_format_mode_line(m) for m in modes)
                    if modes else "(no modes)"
                )

            elif args.command == "get":
                result = _format_mode_detail(_resolve(conn, args.identifier))

            elif args.command == "enable":
                mode = _resolve(conn, args.identifier)
                set_mode_enabled(conn, mode.id, True)
                result = f"enabled mode {mode.id} ({mode.slug})"

            elif args.command == "disable":
                mode = _resolve(conn, args.identifier)
                set_mode_enabled(conn, mode.id, False)
                result = f"disabled mode {mode.id} ({mode.slug})"

            elif args.command == "set":
                mode = _resolve(conn, args.identifier)
                updated = update_mode(conn, mode.id, **_collect_updates(args))
                assert updated is not None
                result = _format_mode_detail(updated)

            elif args.command == "create":
                sim_args: dict[str, Any] = {"food": args.food}
                if args.grid_width is not None:
                    sim_args["grid_width"] = args.grid_width
                if args.grid_height is not None:
                    sim_args["grid_height"] = args.grid_height
                if args.sim_map is not None:
                    sim_args["map"] = args.sim_map
                scoring = {
                    "alpha": args.alpha, "beta": args.beta,
                    "w": args.w, "floor_ms": args.floor_ms,
                }
                new_id = create_mode(
                    conn,
                    slug=args.slug,
                    name=args.name,
                    description=args.description,
                    group_slug=args.group_slug,
                    participant_count=args.participant_count,
                    sim_args=sim_args,
                    map_slug=args.map_slug,
                    budget_ms=args.budget_ms,
                    scoring_config=scoring,
                    target_matches_per_version=args.target_matches_per_version,
                    enabled=not args.disabled,
                )
                created = get_mode(conn, new_id)
                assert created is not None
                result = _format_mode_detail(created)

            elif args.command == "delete":
                mode = _resolve(conn, args.identifier)
                delete_mode(conn, mode.id)
                result = f"deleted mode {mode.id} ({mode.slug})"

            else:
                raise AssertionError("unreachable")

    print(result)


if __name__ == "__main__":
    main()
