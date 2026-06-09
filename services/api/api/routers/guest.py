# services/api/api/routers/guest.py
"""Guest session endpoints.

POST /guest/session  — create-or-refresh a guest session (called on first
                       visit with a client-generated UUID).
POST /guest/claim    — migrate a guest session's projects to the signed-in
                       user's account (called immediately after Clerk sign-in).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Header, status
from psycopg import Connection
from pydantic import BaseModel

from sa_common.db.guest_sessions import (
    GuestSession,
    GUEST_TEST_LIMIT,
    claim_guest_session,
    get_or_create_guest_session,
)
from sa_common.db.users import User

from api.auth import get_current_user
from api.db import get_db
from api.schemas import GuestSessionOut

router = APIRouter(prefix="/guest", tags=["guest"])


@router.post("/session", response_model=GuestSessionOut)
def create_or_refresh_session(
    x_guest_session: str = Header(..., alias="X-Guest-Session"),
    conn: Connection = Depends(get_db),
) -> GuestSessionOut:
    """Create a new guest session or refresh the expiry of an existing one.

    The client generates the UUID locally on first visit and stores it in
    localStorage. Calling this endpoint is optional — get_or_create is called
    lazily on every guest API request — but calling it on load lets the
    frontend display the exact expiry time.
    """
    session = get_or_create_guest_session(conn, x_guest_session)
    return _out(session)


class _ClaimBody(BaseModel):
    session_id: str


@router.post("/claim")
def claim_session(
    body: _ClaimBody,
    conn: Connection = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Migrate all projects from a guest session to the signed-in user.

    Called by the frontend immediately after Clerk sign-in when a guest
    session exists in localStorage. Idempotent: if the session is already
    gone (expired or previously claimed) returns migrated=0.
    """
    migrated = claim_guest_session(conn, body.session_id, user.id)
    return {"migrated": migrated}


def _out(s: GuestSession) -> GuestSessionOut:
    return GuestSessionOut(
        session_id=str(s.session_id),
        test_count=s.test_count,
        test_limit=GUEST_TEST_LIMIT,
        tests_remaining=max(0, GUEST_TEST_LIMIT - s.test_count),
        expires_at=int(s.expires_at.timestamp()),
    )
