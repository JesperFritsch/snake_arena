# services/api/api/rate_limit.py
"""Redis-backed fixed-window rate limiting.

Two kinds of caller use this module:

1. **Per-event quotas** (e.g. 5 submissions/hour, 10 image uploads/day).
   Counters live in Redis because the underlying tables don't record one
   row per event. The fixed-window reset boundary is exposed to the UI so
   it can render "resets at 14:00". Used via `check_quota()`.

2. **General API rate limit** (e.g. 60 req/min per user). Belt-and-suspenders
   behind Cloudflare. On breach we return 429 with a Retry-After header.
   Used via `general_rate_limit` dependency.

Window math is uniform: bucket key = `<prefix>:<id>:<window_start_epoch>`,
INCR + EXPIRE on miss. One round-trip per request via a pipeline.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from fastapi import HTTPException, Request, status
from redis.asyncio import Redis

from api.redis import get_redis


@dataclass(slots=True)
class RateLimitResult:
    """Outcome of a single quota check.

    `reset_at` is the epoch second at which the current fixed window ends —
    i.e. when the count resets to 0 and the user gets the full `limit` back.
    Distinct from sliding-window helpers (sa_common.db.quotas), which expose
    only the next-single-slot timestamp.
    """
    limit: int
    used: int
    remaining: int
    reset_at: int
    window_seconds: int

    def headers(self) -> dict[str, str]:
        return {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(self.remaining),
            "X-RateLimit-Reset": str(self.reset_at),
        }

    def to_json(self) -> dict:
        return {
            "limit": self.limit,
            "used": self.used,
            "remaining": self.remaining,
            "reset_at": self.reset_at,
            "window_seconds": self.window_seconds,
        }


async def _incr_bucket(
    redis: Redis, key: str, window_seconds: int, *, dry_run: bool
) -> tuple[int, int]:
    """Atomically increment (or peek) a fixed-window bucket.

    Returns (used, reset_at_epoch). When dry_run is True we read without
    incrementing — used by quota-peek endpoints so the act of asking how many
    you have left doesn't consume a slot.
    """
    now = int(time.time())
    window_start = now - (now % window_seconds)
    reset_at = window_start + window_seconds
    bucket_key = f"{key}:{window_start}"

    if dry_run:
        raw = await redis.get(bucket_key)
        used = int(raw) if raw is not None else 0
        return used, reset_at

    async with redis.pipeline(transaction=False) as pipe:
        pipe.incr(bucket_key, 1)
        pipe.expire(bucket_key, window_seconds)
        results = await pipe.execute()
    used = int(results[0])
    return used, reset_at


def _raise_429(result: RateLimitResult, message: str) -> None:
    retry_after = max(1, result.reset_at - int(time.time()))
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail={
            "error": "quota_exceeded",
            "message": message,
            **result.to_json(),
        },
        headers={**result.headers(), "Retry-After": str(retry_after)},
    )


async def peek_quota(key: str, limit: int, window_seconds: int) -> RateLimitResult:
    """Read the current bucket without consuming. Never raises."""
    redis = get_redis()
    try:
        used, reset_at = await _incr_bucket(redis, key, window_seconds, dry_run=True)
    finally:
        await redis.aclose()
    return RateLimitResult(
        limit=limit,
        used=used,
        remaining=max(0, limit - used),
        reset_at=reset_at,
        window_seconds=window_seconds,
    )


async def check_quota_available(
    key: str, limit: int, window_seconds: int, *, message: str | None = None
) -> RateLimitResult:
    """Peek and raise 429 when no slots remain. Does not consume.

    Use before an expensive/uncertain operation; pair with consume_quota
    after success so failed operations don't burn a slot.
    """
    result = await peek_quota(key, limit, window_seconds)
    if result.remaining <= 0:
        _raise_429(result, message or f"Limit of {limit} per {window_seconds}s reached")
    return result


async def consume_quota(
    key: str, limit: int, window_seconds: int, *, message: str | None = None
) -> RateLimitResult:
    """Increment the bucket and return the post-increment result.

    Raises 429 if the increment pushed the user over the limit — useful when
    you want the increment itself to be the gate, e.g. for cheap endpoints
    where pre-checking adds an unnecessary round trip.
    """
    redis = get_redis()
    try:
        used, reset_at = await _incr_bucket(redis, key, window_seconds, dry_run=False)
    finally:
        await redis.aclose()
    result = RateLimitResult(
        limit=limit,
        used=used,
        remaining=max(0, limit - used),
        reset_at=reset_at,
        window_seconds=window_seconds,
    )
    if used > limit:
        _raise_429(result, message or f"Limit of {limit} per {window_seconds}s reached")
    return result


# --------------------------------------------------------------------------
# Per-event policy constants (submission, image upload)
# --------------------------------------------------------------------------

SUBMIT_HOURLY_LIMIT = 5
SUBMIT_DAILY_LIMIT = 20
IMAGE_UPLOAD_DAILY_LIMIT = 10
_HOUR = 3600
_DAY = 86400


def _submit_keys(user_id: int) -> tuple[str, str]:
    return f"quota:submit:hour:{user_id}", f"quota:submit:day:{user_id}"


def _upload_key(user_id: int) -> str:
    return f"quota:upload:day:{user_id}"


async def peek_submit_quotas(user_id: int) -> tuple[RateLimitResult, RateLimitResult]:
    hkey, dkey = _submit_keys(user_id)
    hourly = await peek_quota(hkey, SUBMIT_HOURLY_LIMIT, _HOUR)
    daily = await peek_quota(dkey, SUBMIT_DAILY_LIMIT, _DAY)
    return hourly, daily


async def check_submit_quotas_available(user_id: int) -> None:
    hkey, dkey = _submit_keys(user_id)
    await check_quota_available(
        hkey, SUBMIT_HOURLY_LIMIT, _HOUR,
        message=f"Hourly submission limit reached ({SUBMIT_HOURLY_LIMIT}).",
    )
    await check_quota_available(
        dkey, SUBMIT_DAILY_LIMIT, _DAY,
        message=f"Daily submission limit reached ({SUBMIT_DAILY_LIMIT}).",
    )


async def consume_submit_quotas(user_id: int) -> tuple[RateLimitResult, RateLimitResult]:
    hkey, dkey = _submit_keys(user_id)
    hourly = await consume_quota(hkey, SUBMIT_HOURLY_LIMIT, _HOUR)
    daily = await consume_quota(dkey, SUBMIT_DAILY_LIMIT, _DAY)
    return hourly, daily


async def peek_upload_quota(user_id: int) -> RateLimitResult:
    return await peek_quota(_upload_key(user_id), IMAGE_UPLOAD_DAILY_LIMIT, _DAY)


async def check_upload_quota_available(user_id: int) -> None:
    await check_quota_available(
        _upload_key(user_id), IMAGE_UPLOAD_DAILY_LIMIT, _DAY,
        message=f"Daily image-upload limit reached ({IMAGE_UPLOAD_DAILY_LIMIT}).",
    )


async def consume_upload_quota(user_id: int) -> RateLimitResult:
    return await consume_quota(_upload_key(user_id), IMAGE_UPLOAD_DAILY_LIMIT, _DAY)


# --------------------------------------------------------------------------
# General API rate limit
# --------------------------------------------------------------------------

# Authenticated users: identified by user_id. Anonymous reads: identified by
# the client IP (Cloudflare forwards it in CF-Connecting-IP; we fall back to
# X-Forwarded-For then the socket peer for local dev).
_GENERAL_AUTH_LIMIT_PER_MIN = 120
_GENERAL_AUTH_WRITE_LIMIT_PER_MIN = 60
_GENERAL_ANON_LIMIT_PER_MIN = 120


def _client_ip(request: Request) -> str:
    cf = request.headers.get("CF-Connecting-IP")
    if cf:
        return cf
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",", 1)[0].strip()
    return request.client.host if request.client else "unknown"


async def _enforce(key: str, limit: int) -> None:
    """One-shot 429-on-breach, no headers surfaced on the success path. Used by
    the general API limiter where exposing counters to every response would
    be noise."""
    redis = get_redis()
    try:
        used, reset_at = await _incr_bucket(redis, key, 60, dry_run=False)
    finally:
        await redis.aclose()
    if used > limit:
        retry_after = max(1, reset_at - int(time.time()))
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error": "rate_limited", "message": "too many requests"},
            headers={"Retry-After": str(retry_after)},
        )


_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# Endpoints that bypass the general limiter (uptime checks, signed webhooks).
# /webhooks/clerk is Svix-signed, so the signature gates abuse — letting
# Clerk batch-retry a backlog past 30 req/min is fine.
_GENERAL_LIMIT_EXEMPT_PATHS = {"/health", "/webhooks/clerk"}


def _resolve_principal(request: Request) -> tuple[str, bool]:
    """Identify the request for rate-limiting.

    Returns (key, is_authenticated). When a bearer token is present and
    decodes cleanly we key on `sub` (Clerk user id). Anything else — no
    token, expired, malformed — falls back to the client IP. We deliberately
    don't reject invalid tokens here; that's the route's job via
    get_current_user. Worst case an attacker spams with garbage tokens and
    gets rate-limited by IP, same as no token.
    """
    auth = request.headers.get("Authorization")
    if auth and auth.startswith("Bearer "):
        token = auth.removeprefix("Bearer ").strip()
        try:
            # Local import to avoid circular import with api.auth.
            from api.auth import decode_token

            claims = decode_token(token)
            sub = claims.get("sub")
            if sub:
                return f"user:{sub}", True
        except Exception:
            pass
    return f"ip:{_client_ip(request)}", False


async def apply_general_rate_limit(request: Request) -> None:
    """Apply the per-request general rate limit.

    Auth'd users get split read / write buckets; anonymous users share one
    tighter bucket. Returns silently on success; raises 429 with Retry-After
    on breach. The middleware in api.main calls this for every request.
    """
    if request.url.path in _GENERAL_LIMIT_EXEMPT_PATHS:
        return

    principal, is_auth = _resolve_principal(request)
    if is_auth:
        is_write = request.method in _WRITE_METHODS
        limit = _GENERAL_AUTH_WRITE_LIMIT_PER_MIN if is_write else _GENERAL_AUTH_LIMIT_PER_MIN
        bucket = "writes" if is_write else "reads"
        key = f"ratelimit:{principal}:{bucket}"
    else:
        limit = _GENERAL_ANON_LIMIT_PER_MIN
        key = f"ratelimit:{principal}"
    await _enforce(key, limit)
