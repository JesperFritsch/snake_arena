# services/api/api/main.py
"""FastAPI application entrypoint.

Thin HTTP layer over sa_common. No business logic lives here — routes
validate input, check ownership, and call sa_common DB helpers. Match
orchestration and agent builds happen in separate containers that poll the
match_jobs / build_jobs tables.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.db import close_pool, init_pool
from api.routers import jobs, matches, projects
from api.settings import load_settings

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = load_settings()
    init_pool(
        settings.database_url,
        min_size=settings.pool_min_size,
        max_size=settings.pool_max_size,
    )
    log.info("connection pool opened")
    try:
        yield
    finally:
        close_pool()
        log.info("connection pool closed")


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

    app.include_router(jobs.router)
    app.include_router(projects.router)
    app.include_router(matches.router)
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