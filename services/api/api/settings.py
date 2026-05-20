# services/api/api/settings.py
"""Runtime configuration, read once from the environment at import time.

The API is a stateless container; everything it needs comes from env vars so
the same image runs in dev and prod with only the environment differing.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    database_url: str

    # Clerk verification. CLERK_ISSUER is the instance issuer, e.g.
    # https://your-app.clerk.accounts.dev — the JWKS lives at
    # {issuer}/.well-known/jwks.json. CLERK_AUDIENCE is optional; set it only
    # if your Clerk JWT template populates `aud`.
    clerk_issuer: str
    clerk_audience: str | None

    # CORS: the frontend dev origin (e.g. http://localhost:5173). In prod,
    # when FastAPI serves the bundle from the same origin, this can be empty.
    cors_origins: list[str] = field(default_factory=list)

    # Root directory the replay keys (matches.replay_r2_key) are relative to.
    # The runner writes replays here on a shared volume. When replays move to
    # R2, this goes unused and the replay endpoint redirects to a presigned URL
    # instead of reading disk — the key column stays the same.
    replay_dir: Path | None = None
    templates_dir: Path = Path("templates")
    pool_min_size: int = 1
    pool_max_size: int = 8


def get_settings() -> Settings:
    return load_settings()


def load_settings() -> Settings:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError(
            "DATABASE_URL not set. Example: "
            "postgresql://snake_arena:dev_password_change_me@localhost:5432/snake_arena"
        )

    clerk_issuer = os.environ.get("CLERK_ISSUER")
    if not clerk_issuer:
        raise RuntimeError(
            "CLERK_ISSUER not set. This is your Clerk instance issuer URL, e.g. "
            "https://your-app.clerk.accounts.dev"
        )

    return Settings(
        database_url=database_url,
        clerk_issuer=clerk_issuer.rstrip("/"),
        clerk_audience=os.environ.get("CLERK_AUDIENCE") or None,
        cors_origins=_split_csv(os.environ.get("CORS_ORIGINS")),
        replay_dir=Path(os.environ["REPLAY_DIR"]) if os.environ.get("REPLAY_DIR") else None,
        templates_dir=Path(os.environ.get("TEMPLATES_DIR", "code_templates")).resolve(),
        pool_min_size=int(os.environ.get("DB_POOL_MIN_SIZE", "1")),
        pool_max_size=int(os.environ.get("DB_POOL_MAX_SIZE", "8")),
    )