"""Projects: the caller's projects, the live dashboard, its activity feed, the activity evidence
read and the project event stream.

Every route takes the project from the path. The caller's role is their role in that project; a
person outside the project gets 404 for every route, so the project's existence is not revealed.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, Query, Request

from atlas.api.identity import principal_of
from atlas.api.ports import NotWired
from atlas.api.routes_ops import _bind_session, _sse, _stream
from atlas.api.streams import PROJECT_STREAM
from atlas.api.support import ApiError, envelope, services_of
from atlas.feed import NOT_CONNECTED, ProjectFeed
from atlas.projects import ANY_PROJECT

router = APIRouter()


def _member(request: Request, project_id: str) -> Any:
    principal = principal_of(request)
    if principal.project_id not in (project_id, ANY_PROJECT):
        raise ApiError(404, "not_found", "project not found")
    s = services_of(request)
    if s.projects.configured and not s.projects.exists(project_id):
        raise ApiError(404, "not_found", "project not found")
    return principal


def _feed(request: Request) -> ProjectFeed:
    s = services_of(request)
    if s.feed is None:
        s.feed = ProjectFeed(s.hub)
    return s.feed


@router.get("/projects")
def projects(request: Request) -> dict[str, Any]:
    """The projects the caller may switch to, with their data source notes."""
    principal = principal_of(request)
    s = services_of(request)
    identity = s.identity
    provider = getattr(identity, "provider", None)
    if provider is not None and principal.user:
        ids = provider.users.projects_of(principal.user)
    else:
        ids = s.projects.ids()
    # Listed under ``id`` (not ``project_id``): the list is about the caller's own memberships, so the
    # projection must not withhold the other projects the caller belongs to.
    items = []
    for pid in ids:
        d = s.projects.describe(pid)
        items.append({"id": d.pop("project_id"), **d})
    current = None if principal.project_id == ANY_PROJECT else principal.project_id
    return envelope(request, principal, {"projects": items, "current": current})


@router.get("/projects/{project_id}/dashboard")
def dashboard(request: Request, project_id: str) -> dict[str, Any]:
    principal = _member(request, project_id)
    board = _feed(request).dashboard(project_id)
    note = services_of(request).projects.describe(project_id).get("data_source_note", "")
    return envelope(request, principal, {**board, "data_source_note": note}, data_mode=board["data_mode"])


@router.get("/projects/{project_id}/activity")
def activity(request: Request, project_id: str, limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    principal = _member(request, project_id)
    items = _feed(request).activity(project_id, limit)
    return envelope(request, principal, {"project_id": project_id, "activity": items})


@router.get("/projects/{project_id}/evidence/{record_id:path}")
def activity_evidence(request: Request, project_id: str, record_id: str,
                      content_version_id: str | None = Query(None)) -> dict[str, Any]:
    """Project-authorised, version-bound read of a live record behind an activity item.

    This is the one evidence read outside a run's snapshot. It returns exactly the named content
    version even after later updates, and never feeds an investigation."""
    principal = _member(request, project_id)
    s = services_of(request)
    reader = getattr(s.snapshots, "live_evidence", None)
    if reader is None:
        raise ApiError(503, "not_connected", NOT_CONNECTED)
    try:
        rec = reader(project_id, record_id, content_version_id)
    except NotWired as exc:
        raise ApiError(503, "not_connected", str(exc)) from exc
    if rec is None or (rec.get("project_id") not in (None, "", project_id)):
        raise ApiError(404, "not_found", "record not found")
    return envelope(request, principal, {"project_id": project_id, "record": dict(rec)})


@router.get("/projects/{project_id}/stream")
def project_stream(request: Request, project_id: str, follow: bool = Query(True),
                   last_event_id: str | None = Header(None)):
    _member(request, project_id)
    _feed(request)
    return _sse(_stream(request, PROJECT_STREAM, last_event_id, None, follow, _bind_session(request),
                        project_id=project_id))
