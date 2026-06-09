# services/api/api/auth.py
"""Authentication: verify Clerk-issued bearer JWTs and resolve them to a row
in our own `users` table, or accept a guest session header for unauthenticated
access.

Signed-in flow:
  1. Frontend authenticates with Clerk (GitHub OAuth) and gets a short-lived JWT.
  2. It sends the JWT as `Authorization: Bearer <token>` on every API call.
  3. We verify the signature against Clerk's JWKS (cached by PyJWKClient),
     then read `sub` (Clerk user id), `email`, and `name` from the claims.
  4. We find-or-create the matching row in `users`, keyed on clerk_user_id.

Guest flow:
  1. Frontend generates a UUID on first visit and stores it in localStorage.
  2. It sends it as `X-Guest-Session: <uuid>` on every API call.
  3. We find-or-create the matching row in `guest_sessions`.
  4. Guest sessions expire after 48 h; all associated data is cleaned up.

The Clerk JWT template MUST include `email` and `name` claims. Clerk's default
session token carries neither, so configure a JWT template (or add the claims
to the default session token) before this will work. `sub` is always present.
"""
from __future__ import annotations

import functools
from typing import Union

import jwt
from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient
from psycopg import Connection

from sa_common.db.guest_sessions import GuestSession, get_or_create_guest_session
from sa_common.db.users import User, get_or_create_user_by_clerk_id

from api.db import get_db
from api.settings import Settings, load_settings

# Union type for endpoints that serve both signed-in users and guests.
Principal = Union[User, GuestSession]

_bearer = HTTPBearer(auto_error=True)
_optional_bearer = HTTPBearer(auto_error=False)


@functools.lru_cache(maxsize=1)
def _settings() -> Settings:
    return load_settings()


@functools.lru_cache(maxsize=1)
def _jwks_client() -> PyJWKClient:
    issuer = _settings().clerk_issuer
    # PyJWKClient caches fetched signing keys and refreshes on unknown kid.
    return PyJWKClient(f"{issuer}/.well-known/jwks.json")



def decode_token(token: str) -> dict:
    settings = _settings()
    try:
        signing_key = _jwks_client().get_signing_key_from_jwt(token)
        options = {"require": ["exp", "sub"]}
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=settings.clerk_issuer,
            # aud is only verified if the Clerk JWT template sets it.
            audience=settings.clerk_audience,
            options={**options, "verify_aud": settings.clerk_audience is not None},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"invalid token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def _resolve_user(token: str, conn: Connection) -> User:
    claims = decode_token(token)
    clerk_user_id = claims.get("sub")
    email = claims.get("email")
    display_name = claims.get("name") or claims.get("display_name")
    if not clerk_user_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token missing sub claim")
    if not email:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "token missing email claim — add it to your Clerk JWT template",
        )
    return get_or_create_user_by_clerk_id(
        conn,
        clerk_user_id=clerk_user_id,
        email=email,
        display_name=display_name or email,
    )


def get_current_user(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
    conn: Connection = Depends(get_db),
) -> User:
    return _resolve_user(creds.credentials, conn)


def get_principal(
    creds: HTTPAuthorizationCredentials | None = Depends(_optional_bearer),
    x_guest_session: str | None = Header(None, alias="X-Guest-Session"),
    conn: Connection = Depends(get_db),
) -> Principal:
    """Resolve a signed-in user or a guest session.

    JWT takes precedence when both headers are present. If the JWT is
    invalid or expired (e.g. Clerk hasn't cleared the token yet after
    sign-out) and a guest session header is also present, we fall through
    to the guest session rather than hard-failing. This lets the client
    work correctly during the brief window between sign-out and Clerk
    fully clearing its cached token.
    """
    if creds is not None:
        try:
            return _resolve_user(creds.credentials, conn)
        except HTTPException:
            if not x_guest_session:
                raise
            # Bad/expired JWT but guest session present — treat as guest.
    if x_guest_session:
        return get_or_create_guest_session(conn, x_guest_session)
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required")