# services/sa_common/sa_common/db/quotas.py
"""Per-user queue quotas computed from DB event tables.

A "queue quota" is a count of expensive jobs a user has created inside a
rolling time window. Unlike a request-rate limit (which counts HTTP calls in
Redis), this counts rows in a jobs table and is naturally sliding: the oldest
in-window job ages out second by second.

Only tables that record every event (user_id + timestamp per row) can use this
pattern. test_match_jobs qualifies; promote_to_submitted does not (it only
bumps projects.submitted_at). For the latter we use Redis fixed-window
counters in the API layer.

Returns a QuotaWindow with `next_slot_at` set to when the user gets ONE more
slot back — not when the full limit refreshes. With a sliding window there is
no single reset boundary.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import psycopg


@dataclass(slots=True)
class QuotaWindow:
    limit: int
    used: int
    remaining: int
    next_slot_at: datetime | None
    window_seconds: int


TEST_MATCH_HOURLY_LIMIT = 120
_TEST_MATCH_WINDOW_SECONDS = 3600


def get_test_match_quota(conn: psycopg.Connection, user_id: int) -> QuotaWindow:
    """Sliding-window count of the user's test matches in the last hour.

    next_slot_at is min(requested_at) + interval '1 hour' across in-window
    rows: the moment the oldest currently-counted job falls out of the window
    and gives the user one slot back. None when used = 0.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*),
                   min(requested_at) + interval '1 hour'
            FROM test_match_jobs
            WHERE requested_by = %s
              AND requested_at > now() - interval '1 hour'
            """,
            (user_id,),
        )
        row = cur.fetchone()
        assert row is not None
        used = int(row[0])
        next_slot_at = row[1] if used > 0 else None
    return QuotaWindow(
        limit=TEST_MATCH_HOURLY_LIMIT,
        used=used,
        remaining=max(0, TEST_MATCH_HOURLY_LIMIT - used),
        next_slot_at=next_slot_at,
        window_seconds=_TEST_MATCH_WINDOW_SECONDS,
    )
