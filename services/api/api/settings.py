# services/api/api/settings.py
"""Runtime configuration, read once from the environment at import time.

The API is a stateless container; everything it needs comes from env vars so
the same image runs in dev and prod with only the environment differing.
"""
from __future__ import annotations

import functools
import os
from dataclasses import dataclass
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

    # Svix signing secret for Clerk webhooks (the "Signing secret" shown on
    # the endpoint page in Clerk dashboard, format: whsec_…). Used by
    # /webhooks/clerk to verify Clerk-fired events such as user.deleted.
    clerk_webhook_secret: str

    # CORS: the frontend dev origin (e.g. http://localhost:5173). In prod,
    # when FastAPI serves the bundle from the same origin, this can be empty.
    cors_origins: list[str]

    # Base URL where match bundles are served. In dev this points at the nginx
    # file-server container (e.g. http://localhost:8081). In prod set it to the
    # R2 public URL or a presigned-URL base — the bundle endpoint returns
    # {replay_host}/{bundle_path} and the browser fetches directly.
    replay_host: str | None
    templates_dir: Path
    sandbox_images_dir: Path
    redis_url: str
    pool_min_size: int
    pool_max_size: int


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()


def load_settings() -> Settings:
    """Build Settings from the environment. Every required var must be set —
    no in-code defaults. See docker-compose.yml and .env for the contract."""
    return Settings(
        database_url=os.environ["DATABASE_URL"],
        clerk_issuer=os.environ["CLERK_ISSUER"].rstrip("/"),
        # CLERK_AUDIENCE is optional in the protocol; treat empty string as unset.
        clerk_audience=os.environ.get("CLERK_AUDIENCE") or None,
        clerk_webhook_secret=os.environ["CLERK_WEBHOOK_SECRET"],
        cors_origins=_split_csv(os.environ.get("CORS_ORIGINS")),
        # REPLAY_HOST is required for the API (browser fetches use it), but
        # not for orchestrator daemons that only put/get internally.
        replay_host=os.environ["REPLAY_HOST"],
        templates_dir=Path(os.environ["TEMPLATES_DIR"]).resolve(),
        sandbox_images_dir=Path(os.environ["SANDBOX_IMAGES_DIR"]).resolve(),
        redis_url=os.environ["REDIS_URL"],
        pool_min_size=int(os.environ["DB_POOL_MIN_SIZE"]),
        pool_max_size=int(os.environ["DB_POOL_MAX_SIZE"]),
    )