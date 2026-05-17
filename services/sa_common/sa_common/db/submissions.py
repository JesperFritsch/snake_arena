# services/sa_common/sa_common/db/submissions.py
"""Data layer for the submissions table.

A submission is a frozen build of a project: its code at the moment of build,
plus the Docker image_tag the builder produced. The agent's display name and
language come from the project (via project_id), so they're not stored here.

Lifecycle:
    building -> ready    (build succeeded)
    building -> failed   (build failed)
    ready    -> gc'd     (image was garbage collected)

The runner looks up `submission_id` by `image_tag` when recording match
participants — that's the inverse mapping from "what played in this match"
back to "who built it."
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from psycopg import Connection
from psycopg.rows import class_row


SubmissionStatus = Literal["building", "ready", "failed", "gc'd"]


@dataclass
class Submission:
    id: int
    project_id: int
    image_tag: str
    code_archive: bytes
    status: str
    created_at: datetime


@dataclass
class SubmissionMeta:
    """Submission without the code archive — cheap for listings and lookups."""
    id: int
    project_id: int
    image_tag: str
    status: str
    created_at: datetime


def create_submission(
    conn: Connection,
    project_id: int,
    image_tag: str,
    code_archive: bytes,
    status: SubmissionStatus = "building",
) -> int:
    """Insert a submission row. Returns the new submission id."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO submissions (project_id, image_tag, code_archive, status)
            VALUES (%s, %s, %s, %s)
            RETURNING id
            """,
            (project_id, image_tag, code_archive, status),
        )
        row = cur.fetchone()
        assert row is not None
        return row[0]


def update_submission_status(
    conn: Connection, submission_id: int, status: SubmissionStatus
) -> None:
    """Transition a submission's status (e.g. building -> ready)."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE submissions SET status = %s WHERE id = %s",
            (status, submission_id),
        )


def get_submission(conn: Connection, submission_id: int) -> Submission | None:
    """Fetch a submission including its code archive."""
    with conn.cursor(row_factory=class_row(Submission)) as cur:
        cur.execute("SELECT * FROM submissions WHERE id = %s", (submission_id,))
        return cur.fetchone()


def get_submission_meta_by_image_tag(
    conn: Connection, image_tag: str
) -> SubmissionMeta | None:
    """Look up submission metadata by Docker image tag.

    Used by the runner to resolve image_tag -> submission_id when recording
    match participants.
    """
    with conn.cursor(row_factory=class_row(SubmissionMeta)) as cur:
        cur.execute(
            """
            SELECT id, project_id, image_tag, status, created_at
            FROM submissions WHERE image_tag = %s
            """,
            (image_tag,),
        )
        return cur.fetchone()


def list_submissions_for_project(
    conn: Connection, project_id: int
) -> list[SubmissionMeta]:
    """List a project's submissions, newest first, without code archives."""
    with conn.cursor(row_factory=class_row(SubmissionMeta)) as cur:
        cur.execute(
            """
            SELECT id, project_id, image_tag, status, created_at
            FROM submissions WHERE project_id = %s
            ORDER BY created_at DESC
            """,
            (project_id,),
        )
        return cur.fetchall()