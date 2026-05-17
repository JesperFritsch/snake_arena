# services/submitter/submitter/submit.py
"""Promote a project's current dev build to a numbered submitted version.

Submit is a deliberate user action — separate from save (which only touches
the editor draft) and from test-build (which only updates the dev image).
It re-tags the existing dev image under a versioned tag, then atomically
records the promotion in the database.

The Docker retag happens BEFORE the DB update so that a failed retag never
leaves a DB row pointing at a missing image. If the DB update is then
refused (preconditions not met), we clean up the stray tag.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import docker
from docker.errors import ImageNotFound

from sa_common.db.connection import get_conn
from sa_common.db.projects import (
    ProjectMeta,
    get_project_meta,
    promote_to_submitted,
)

log = logging.getLogger(__name__)


class SubmitError(Exception):
    """A submit was refused. The reason should be user-presentable."""


@dataclass
class SubmitResult:
    new_version: int
    submitted_image_tag: str


def _safe_name(name: str) -> str:
    """Sanitize a project name for use in a Docker tag.

    Same convention as the builder uses for build-time image tags.
    """
    return "".join(c if c.isalnum() or c in "-_." else "-" for c in name.lower())


def _build_submitted_tag(project: ProjectMeta, new_version: int) -> str:
    return f"snake-{project.user_id}-{_safe_name(project.name)}:v{new_version}"


def submit_project(project_id: int) -> SubmitResult:
    """Promote the project's current dev build to a new submitted version.

    Raises:
        SubmitError: if the project can't be submitted right now. The
            message is suitable for showing to the user.

    Returns:
        SubmitResult with the new version number and the tag the image
        was promoted under.
    """
    client = docker.from_env()

    # Read current state. We need user_id + name to compute the tag, and
    # submitted_version to know what the next number will be.
    with get_conn(autocommit=True) as conn:
        project = get_project_meta(conn, project_id)

    if project is None:
        raise SubmitError(f"project {project_id} not found")
    if project.source != "browser":
        # External-image projects don't use the dev/submit flow — they get
        # submitted via a different path. Be explicit so callers don't
        # silently no-op.
        raise SubmitError("only browser projects can be submitted via this flow")
    if project.dev_build_status != "ready":
        raise SubmitError(
            "dev build is not ready — run a test first "
            f"(current status: {project.dev_build_status or 'never built'})"
        )
    if project.dev_image_tag is None:
        # Defensive — dev_build_status='ready' should always imply tag is set.
        raise SubmitError("dev build has no image tag")
    if project.updated_at > (project.dev_built_at or project.updated_at):
        raise SubmitError(
            "code has changed since the last test build — test your "
            "changes before submitting"
        )

    new_version = project.submitted_version + 1
    new_tag = _build_submitted_tag(project, new_version)

    # Docker retag first. If this fails we abort cleanly without touching
    # the DB. A "fail" here is almost always "dev image no longer exists"
    # (someone GC'd it manually).
    try:
        dev_image = client.images.get(project.dev_image_tag)
    except ImageNotFound:
        raise SubmitError(
            f"dev image {project.dev_image_tag!r} is missing from the "
            "local Docker store — rebuild and try again"
        )

    repo, _, _ = new_tag.partition(":")
    tag = new_tag.split(":", 1)[1]
    dev_image.tag(repository=repo, tag=tag)
    log.info("tagged dev image as %s", new_tag)

    # DB update. If preconditions fail (dev not ready, or code changed
    # between our read above and now), promote_to_submitted returns None.
    # Untag and surface the failure.
    with get_conn(autocommit=True) as conn:
        committed_version = promote_to_submitted(conn, project_id, new_tag)

    if committed_version is None:
        log.warning(
            "promote_to_submitted refused for project %d; removing tag %s",
            project_id, new_tag,
        )
        _untag(client, new_tag)
        raise SubmitError(
            "submit was refused — your code changed between reading the "
            "project state and writing the new version (rare race; try again)"
        )

    log.info(
        "promoted project %d to v%d (tag=%s)",
        project_id, committed_version, new_tag,
    )
    return SubmitResult(new_version=committed_version, submitted_image_tag=new_tag)


def _untag(client: "docker.DockerClient", tag: str) -> None:
    """Remove a tag we just added. Best-effort; failure is logged not raised."""
    try:
        # remove_image(tag) only removes the tag if the image has other tags;
        # since the dev image still has its :dev tag, this just untags :v{n}.
        client.images.remove(tag)
    except Exception as e:
        log.warning("failed to remove tag %s: %s", tag, e)