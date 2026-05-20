# services/api/api/auth.py
"""Authentication: verify Clerk-issued bearer JWTs and resolve them to a row
in our own `users` table.

Flow:
  1. Frontend authenticates with Clerk (GitHub OAuth) and gets a short-lived JWT.
  2. It sends the JWT as `Authorization: Bearer <token>` on every API call.
  3. We verify the signature against Clerk's JWKS (cached by PyJWKClient),
     then read `sub` (Clerk user id), `email`, and `name` from the claims.
  4. We find-or-create the matching row in `users`, keyed on clerk_user_id.

The Clerk JWT template MUST include `email` and `name` claims. Clerk's default
session token carries neither, so configure a JWT template (or add the claims
to the default session token) before this will work. `sub` is always present.
"""
from __future__ import annotations

import functools

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient
from psycopg import Connection

from sa_common.db.users import User, get_or_create_user_by_clerk_id

from api.db import get_db
from api.settings import Settings, load_settings

_bearer = HTTPBearer(auto_error=True)


@functools.lru_cache(maxsize=1)
def _settings() -> Settings:
    return load_settings()


@functools.lru_cache(maxsize=1)
def _jwks_client() -> PyJWKClient:
    issuer = _settings().clerk_issuer
    # PyJWKClient caches fetched signing keys and refreshes on unknown kid.
    return PyJWKClient(f"{issuer}/.well-known/jwks.json")


def _decode(token: str) -> dict:
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


def get_current_user(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
    conn: Connection = Depends(get_db),
) -> User:
    claims = _decode(creds.credentials)

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