# services/api/api/db.py
"""Connection pooling for the API.

This is the first place in the codebase that needs a pool: the runner opens
one connection per match, but the API serves many concurrent short requests.
`sa_common`'s DB helpers all take an explicit `conn`, so the pool is purely an
API concern — we hand pooled connections straight into those helpers.
"""
from __future__ import annotations

from collections.abc import Iterator

import psycopg
from psycopg_pool import ConnectionPool

# Set by the app's lifespan handler at startup; closed at shutdown.
_pool: ConnectionPool | None = None


def init_pool(conninfo: str, *, min_size: int, max_size: int) -> ConnectionPool:
    global _pool
    if _pool is not None:
        return _pool
    _pool = ConnectionPool(
        conninfo,
        min_size=min_size,
        max_size=max_size,
        open=False,
        kwargs={"autocommit": False},
    )
    _pool.open()
    _pool.wait()
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def get_db() -> Iterator[psycopg.Connection]:
    """FastAPI dependency: lease a connection for the duration of one request.

    The pool's context manager commits when the request handler returns
    normally and rolls back if it raises, then returns the connection to the
    pool either way.
    """
    if _pool is None:
        raise RuntimeError("connection pool not initialised")
    with _pool.connection() as conn:
        yield conn