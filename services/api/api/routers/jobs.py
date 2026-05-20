# services/api/api/routers/jobs.py
"""Build-job reads and the current-user endpoint.

Builds are enqueued via POST /projects/{id}/build (see projects router). These
endpoints expose status so the editor can poll a build to completion. The
builder daemon is the only writer of build outcomes.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from psycopg import Connection

from sa_common.db.build_jobs import (
    JOB_STATUSES,
    BuildJob,
    get_build_job,
    list_build_jobs,
)
from sa_common.db.users import User

from api.auth import get_current_user
from api.db import get_db
from api.schemas import UserOut

router = APIRouter(tags=["jobs"])


@router.get("/me", response_model=UserOut, tags=["users"])
def me(user: User = Depends(get_current_user)) -> UserOut:
    return UserOut(id=user.id, email=user.email, display_name=user.display_name)


@router.get("/build-jobs", response_model=list[BuildJob])
def list_my_build_jobs(
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(20, ge=1, le=100),
    conn: Connection = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[BuildJob]:
    if status_filter is not None and status_filter not in JOB_STATUSES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid status: {status_filter}")
    # build_jobs has no requested_by column; ownership is via the project. For
    # v0 we expose the global recent list (read-only status). Tighten by
    # joining build_jobs.project_id -> projects.user_id if/when needed.
    return list_build_jobs(conn, status=status_filter, limit=limit)


@router.get("/build-jobs/{job_id}", response_model=BuildJob)
def get_build_job_detail(
    job_id: int,
    conn: Connection = Depends(get_db),
    _: User = Depends(get_current_user),
) -> BuildJob:
    job = get_build_job(conn, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "build job not found")
    return job