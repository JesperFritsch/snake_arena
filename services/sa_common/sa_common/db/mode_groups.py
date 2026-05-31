# services/sa_common/sa_common/db/mode_groups.py
"""DB layer for mode groups.

A group is a leaderboard-facing label that bundles multiple modes together —
e.g. one "Solo" group containing several solo-on-different-maps modes. The
group appears as one tab on the leaderboard, with sub-tabs to drill into each
constituent mode. The overall leaderboard normalises each group as one unit
(otherwise N solo maps would outweigh M multi-modes).

Modes that don't reference a group (group_slug NULL) render as their own
standalone tab — same as today's pre-group behavior.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row

from sa_common.db.connection import get_conn


@dataclass(slots=True)
class Group:
    slug: str
    name: str
    description: str | None
    sort_order: int


_GROUP_COLUMNS = "slug, name, description, sort_order"


def _row_to_group(row: dict[str, Any]) -> Group:
    return Group(
        slug=row["slug"],
        name=row["name"],
        description=row["description"],
        sort_order=row["sort_order"],
    )


def list_groups(conn: psycopg.Connection) -> list[Group]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"SELECT {_GROUP_COLUMNS} FROM mode_groups "
            "ORDER BY sort_order, slug"
        )
        return [_row_to_group(r) for r in cur.fetchall()]


def get_group(conn: psycopg.Connection, slug: str) -> Group | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"SELECT {_GROUP_COLUMNS} FROM mode_groups WHERE slug = %s",
            (slug,),
        )
        row = cur.fetchone()
        return _row_to_group(row) if row else None


def create_group(
    conn: psycopg.Connection,
    *,
    slug: str,
    name: str,
    description: str | None = None,
    sort_order: int = 0,
) -> Group:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO mode_groups (slug, name, description, sort_order)
            VALUES (%s, %s, %s, %s)
            """,
            (slug, name, description, sort_order),
        )
    g = get_group(conn, slug)
    assert g is not None
    return g


def update_group(
    conn: psycopg.Connection,
    slug: str,
    *,
    name: str | None = None,
    description: str | None = None,
    sort_order: int | None = None,
) -> Group | None:
    sets: list[str] = []
    params: list[Any] = []
    if name is not None:
        sets.append("name = %s")
        params.append(name)
    if description is not None:
        sets.append("description = %s")
        params.append(description)
    if sort_order is not None:
        sets.append("sort_order = %s")
        params.append(sort_order)
    if not sets:
        return get_group(conn, slug)
    params.append(slug)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE mode_groups SET {', '.join(sets)} WHERE slug = %s",
            params,
        )
    return get_group(conn, slug)


def delete_group(conn: psycopg.Connection, slug: str) -> bool:
    """Delete a group. Modes that referenced it get group_slug = NULL
    (via ON DELETE SET NULL) and revert to standalone tabs."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM mode_groups WHERE slug = %s", (slug,))
        return cur.rowcount > 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _format_group_line(g: Group) -> str:
    return f"[{g.sort_order:>4}] {g.slug:<14} {g.name}"


def _format_group_detail(g: Group) -> str:
    return "\n".join([
        f"slug:        {g.slug}",
        f"name:        {g.name}",
        f"description: {g.description}",
        f"sort_order:  {g.sort_order}",
    ])


def cli(argv) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage leaderboard mode groups.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="list all groups")

    get_p = sub.add_parser("get", help="show one group")
    get_p.add_argument("slug")

    create_p = sub.add_parser("create", help="create a new group")
    create_p.add_argument("slug")
    create_p.add_argument("--name", required=True)
    create_p.add_argument("--description")
    create_p.add_argument("--sort-order", type=int, default=0)

    set_p = sub.add_parser("set", help="update group fields")
    set_p.add_argument("slug")
    set_p.add_argument("--name")
    set_p.add_argument("--description")
    set_p.add_argument("--sort-order", type=int)

    delete_p = sub.add_parser(
        "delete",
        help="delete a group; modes that referenced it become ungrouped",
    )
    delete_p.add_argument("slug")

    return parser.parse_args(argv)


def main():
    args = cli(sys.argv[1:])
    with get_conn(autocommit=False) as conn:
        with conn.transaction():
            if args.command == "list":
                groups = list_groups(conn)
                result = (
                    "\n".join(_format_group_line(g) for g in groups)
                    if groups else "(no groups)"
                )

            elif args.command == "get":
                g = get_group(conn, args.slug)
                if g is None:
                    raise SystemExit(f"group {args.slug!r} not found")
                result = _format_group_detail(g)

            elif args.command == "create":
                g = create_group(
                    conn,
                    slug=args.slug,
                    name=args.name,
                    description=args.description,
                    sort_order=args.sort_order,
                )
                result = _format_group_detail(g)

            elif args.command == "set":
                g = update_group(
                    conn,
                    args.slug,
                    name=args.name,
                    description=args.description,
                    sort_order=args.sort_order,
                )
                if g is None:
                    raise SystemExit(f"group {args.slug!r} not found")
                result = _format_group_detail(g)

            elif args.command == "delete":
                if not delete_group(conn, args.slug):
                    raise SystemExit(f"group {args.slug!r} not found")
                result = f"deleted group {args.slug}"

            else:
                raise AssertionError("unreachable")

    print(result)


if __name__ == "__main__":
    main()
