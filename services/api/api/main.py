# services/api/api/main.py
"""FastAPI application entrypoint.

Thin HTTP layer over sa_common. No business logic lives here — routes
validate input, check ownership, and call sa_common DB helpers. Match
orchestration happens in separate containers that poll the match_jobs /
test_match_jobs tables. Agent builds are triggered inline by the test runner.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.db import close_pool, init_pool
from api.rate_limit import apply_general_rate_limit
from api.redis import close_redis, init_redis
from api.routers import users, matches, modes, projects, test_matches, download, leaderboard, webhooks, maps, guest
from api.routers.projects import stale_upload_cleanup_task
from api.settings import load_settings, get_settings, Settings

log = logging.getLogger(__name__)


async def _guest_cleanup_task() -> None:
    """Hourly background task: delete expired guest sessions and their artefacts."""
    import docker as docker_mod
    from sa_common.db.connection import get_conn
    from sa_common.db.guest_sessions import collect_and_delete_expired_sessions

    while True:
        await asyncio.sleep(3600)
        try:
            with get_conn(autocommit=True) as conn:
                image_tags, bundle_keys = collect_and_delete_expired_sessions(conn)

            from api.bundler import get_bundler
            bundler = get_bundler()
            for key in bundle_keys:
                try:
                    bundler.delete(key)
                except Exception:
                    log.warning("failed to delete guest bundle %s", key, exc_info=True)

            if image_tags:
                try:
                    d = docker_mod.from_env()
                    for tag in image_tags:
                        try:
                            d.images.remove(tag, force=True)
                        except Exception:
                            log.warning("failed to remove guest image %s", tag, exc_info=True)
                except Exception:
                    log.warning("docker unavailable for guest image cleanup", exc_info=True)
        except Exception:
            log.exception("guest cleanup task failed")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = load_settings()
    init_pool(
        settings.database_url,
        min_size=settings.pool_min_size,
        max_size=settings.pool_max_size,
    )
    init_redis(settings.redis_url)
    upload_task = asyncio.create_task(stale_upload_cleanup_task())
    guest_task = asyncio.create_task(_guest_cleanup_task())
    log.info("connection pool and Redis pool opened")
    try:
        yield
    finally:
        upload_task.cancel()
        guest_task.cancel()
        close_pool()
        await close_redis()
        log.info("connection pool and Redis pool closed")


def create_app() -> FastAPI:
    settings = load_settings()
    app = FastAPI(title="Gridsnake API", version="0.1.0", lifespan=lifespan)

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.middleware("http")
    async def rate_limit_middleware(request: Request, call_next):
        try:
            await apply_general_rate_limit(request)
        except HTTPException as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail},
                headers=exc.headers or {},
            )
        return await call_next(request)

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
                with open(manifest_path, "rb") as f:
                    data = tomllib.load(f)
                lang = data["language"]
                version_map[lang["name"]] = lang["version"]
        return [
            {"name": p.name, "version": version_map.get(p.name)}
            for p in sorted(templates_dir.iterdir())
            if p.is_dir()
        ]

    app.include_router(users.router)
    app.include_router(guest.router)
    app.include_router(projects.router)
    app.include_router(matches.router)
    app.include_router(modes.router)
    app.include_router(test_matches.router)
    app.include_router(download.router)
    app.include_router(leaderboard.router)
    app.include_router(webhooks.router)
    app.include_router(maps.router)
    return app


app = create_app()


def main() -> None:
    """Console-script entrypoint: `api` runs uvicorn for local dev."""
    import os

    import uvicorn

    uvicorn.run(
        "api.main:app",
        host=os.environ["API_HOST"],
        port=int(os.environ["API_PORT"]),
        # API_RELOAD is a presence flag — any non-empty value enables reload.
        reload=bool(os.environ.get("API_RELOAD")),
    )


if __name__ == "__main__":
    main()