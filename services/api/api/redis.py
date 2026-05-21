# services/api/api/redis.py
"""Async Redis client lifecycle for the API."""
from __future__ import annotations

import redis.asyncio as aioredis

_pool: aioredis.ConnectionPool | None = None


def init_redis(url: str) -> None:
    global _pool
    _pool = aioredis.ConnectionPool.from_url(url, decode_responses=False)


async def close_redis() -> None:
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None


def get_redis() -> aioredis.Redis:
    if _pool is None:
        raise RuntimeError("Redis pool not initialised")
    return aioredis.Redis(connection_pool=_pool)
