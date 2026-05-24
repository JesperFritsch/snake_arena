# services/api/api/main.py
"""FastAPI application entrypoint.

Thin HTTP layer over sa_common. No business logic lives here — routes
validate input, check ownership, and call sa_common DB helpers. Match
orchestration happens in separate containers that poll the match_jobs /
test_match_jobs tables. Agent builds are triggered inline by the test runner.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.db import close_pool, init_pool
from api.redis import close_redis, init_redis
from api.routers import users, matches, projects, test_matches, download
from api.settings import load_settings, get_settings, Settings

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = load_settings()
    init_pool(
        settings.database_url,
        min_size=settings.pool_min_size,
        max_size=settings.pool_max_size,
    )
    init_redis(settings.redis_url)
    log.info("connection pool and Redis pool opened")
    try:
        yield
    finally:
        close_pool()
        await close_redis()
        log.info("connection pool and Redis pool closed")


def create_app() -> FastAPI:
    settings = load_settings()
    app = FastAPI(title="Snake Arena API", version="0.1.0", lifespan=lifespan)

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.get("/health", tags=["meta"])
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/languages", tags=["meta"])
    def list_languages(settings: Settings = Depends(get_settings)) -> list[dict]:
        """Return languages that have starter templates, with name and version."""
        import tomllib
        templates_dir = settings.templates_dir
        sandbox_dir = settings.sandbox_images_dir
        if not templates_dir.is_dir():
            return []
        # Build version map keyed by manifest `name`, not directory name,
        # so sandbox-images/js/ with name="javascript" resolves correctly.
        version_map: dict[str, str] = {}
        if sandbox_dir.is_dir():
            for manifest_path in sandbox_dir.glob("*/manifest.toml"):
                try:
                    with open(manifest_path, "rb") as f:
                        data = tomllib.load(f).get("language", {})
                    if (name := data.get("name")) and (ver := data.get("version")):
                        version_map[name] = ver
                except Exception:
                    pass
        return [
            {"name": p.name, "version": version_map.get(p.name)}
            for p in sorted(templates_dir.iterdir())
            if p.is_dir()
        ]

    app.include_router(users.router)
    app.include_router(projects.router)
    app.include_router(matches.router)
    app.include_router(test_matches.router)
    app.include_router(download.router)
    return app


app = create_app()


def main() -> None:
    """Console-script entrypoint: `api` runs uvicorn for local dev."""
    import os

    import uvicorn

    uvicorn.run(
        "api.main:app",
        host=os.environ.get("API_HOST", "0.0.0.0"),
        port=int(os.environ.get("API_PORT", "8000")),
        reload=bool(os.environ.get("API_RELOAD")),
    )


if __name__ == "__main__":
    main()