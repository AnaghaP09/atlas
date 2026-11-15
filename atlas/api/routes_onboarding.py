"""Add project: detect a public repository's settings, add it, and build "Data as of" copies.

Only the audit role adds projects and builds copies. Every change carries an Idempotency-Key and is
written to the audit record. A new project never gets a publish target or a notification audience.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Request

from atlas.api.identity import principal_of, require_role
from atlas.api.support import ApiError, envelope, idempotent, services_of
from atlas.datacopy import CopyBuilder, CopyRefused, release_tags
from atlas.onboarding import OnboardingError, add_to_projects_file, detect, register_live, validate_project
from atlas.projects import ANY_PROJECT

router = APIRouter()
NOT_CONNECTED = "the data layer is not connected on this server, so repositories cannot be read"


def _audit(request: Request, action: str) -> Any:
    principal = principal_of(request)
    require_role(principal, {"audit"}, action)
    return principal


def _data(s: Any) -> Any:
    wiring = getattr(s, "wiring", None)
    data = (getattr(wiring, "extra", {}) or {}).get("data") if wiring is not None else None
    if data is None:
        raise ApiError(503, "not_connected", NOT_CONNECTED)
    return data


def _projects_path(s: Any) -> Path:
    path = getattr(s, "projects_file", None)
    if not path:
        raise ApiError(503, "not_connected", "this server has no projects file to write to")
    return Path(path)


def _authenticated(data: Any) -> bool:
    supplier = data.gateway._tokens.get("github")
    return bool(supplier and supplier())


def _fail(exc: OnboardingError | CopyRefused) -> ApiError:
    if isinstance(exc, OnboardingError):
        return ApiError(exc.status, exc.error, exc.reason)
    return ApiError(409, "refused", exc.reason)


@router.get("/projects/add/defaults")
def add_defaults(request: Request) -> dict[str, Any]:
    """The link to pre-fill (``ATLAS_ADD_PROJECT``) and whether a GitHub token is set."""
    principal = _audit(request, "add projects")
    s = services_of(request)
    wiring = getattr(s, "wiring", None)
    data = (getattr(wiring, "extra", {}) or {}).get("data") if wiring is not None else None
    return envelope(request, principal, {"repository": getattr(s, "add_project_prefill", "") or "",
                                         "connected": data is not None,
                                         "github_token": bool(data is not None and _authenticated(data))})


@router.post("/projects/detect")
def detect_project(request: Request, body: dict[str, Any] = Body(...)):
    """Read a public repository and propose its project block (nothing is written)."""
    principal = _audit(request, "add projects")
    s = services_of(request)
    data = _data(s)
    repo = str(body.get("repository") or "")

    def effect(key: str) -> tuple[int, dict[str, Any]]:
        try:
            proposal = detect(data.gateway, repo, existing={pid: s.projects.get(pid) for pid in s.projects.ids()})
        except OnboardingError as exc:
            raise _fail(exc) from exc
        s.audit.record(principal.role.value, "project.detect", proposal["repository"], {"repository": repo})
        return 200, envelope(request, principal, proposal)

    return idempotent(request, principal, body, effect)


@router.post("/projects", status_code=201)
def add_project(request: Request, body: dict[str, Any] = Body(...)):
    """Write a confirmed project to the projects file and start watching it."""
    principal = _audit(request, "add projects")
    s = services_of(request)
    path = _projects_path(s)

    def effect(key: str) -> tuple[int, dict[str, Any]]:
        try:
            existing = {pid: s.projects.get(pid) for pid in s.projects.ids()}
            project = validate_project(body.get("project") or {}, existing)
            block = add_to_projects_file(path, project)
        except OnboardingError as exc:
            raise _fail(exc) from exc
        live = register_live(s, project)
        s.audit.record(principal.role.value, "project.add", project["project_id"],
                       {"repository": project["repository"], "labels": project["labels"], "ci": project["ci"]})
        # Under ``id``: the caller acts in another project, and an object naming this project would be withheld.
        return 201, envelope(request, principal, {"id": project["project_id"], "name": project["name"],
                                                  "repository": project["repository"], "block": block,
                                                  "publish_targets": [], "live": live})

    return idempotent(request, principal, body, effect)


def _member(request: Request, project_id: str) -> Any:
    principal = _audit(request, "build data copies")
    if principal.project_id not in (project_id, ANY_PROJECT):
        raise ApiError(404, "not_found", "project not found")
    s = services_of(request)
    if s.projects.configured and not s.projects.exists(project_id):
        raise ApiError(404, "not_found", "project not found")
    return principal


def _builder(s: Any) -> CopyBuilder:
    builder = getattr(s, "data_copies", None)
    if builder is not None:
        return builder
    data = _data(s)

    def project(pid: str) -> dict[str, Any] | None:
        cfg = data.projects.get(pid)
        if cfg is None:
            return None
        named = s.projects.get(pid)
        return {"repository": cfg.sources.repository, "labels": dict(cfg.labels),
                "security_inventory": cfg.security_inventory, "bot_accounts": list(cfg.sources.bot_accounts),
                "name": getattr(named, "name", "") or pid}

    def done(job: Any) -> None:
        s.audit.record("audit", f"data_copy.{job.state}", job.snapshot_id or job.job_id,
                       {"project_id": job.project_id, "tag": job.tag, "github_reads": job.requests, "message": job.message})

    builder = CopyBuilder(data_dir=Path(data.registry.data_dir), gateway=data.gateway, registry=data.registry,
                          project=project, authenticated=lambda: _authenticated(data), on_done=done)
    s.data_copies = builder
    return builder


@router.get("/projects/{project_id}/release-tags")
def project_release_tags(request: Request, project_id: str) -> dict[str, Any]:
    """Release tags of the project to build a copy from, newest first."""
    principal = _member(request, project_id)
    s = services_of(request)
    data = _data(s)
    cfg = data.projects.get(project_id)
    if cfg is None or not cfg.sources.repository:
        raise ApiError(404, "not_found", "this project has no repository to read")
    try:
        tags = release_tags(data.gateway, cfg.sources.repository, cfg.release_tag_pattern)
    except Exception as exc:  # a failed read is shown, never a crash
        raise ApiError(503, "not_connected", f"the release tags could not be read: {exc}") from exc
    built = {snap.get("snapshot_id") for snap in data.registry.list_snapshots(project_id)}
    from atlas.datacopy import copy_id

    for t in tags:
        t["copy_exists"] = copy_id(project_id, t["tag"]) in built
    return envelope(request, principal, {"project_id": project_id, "tags": tags})


@router.post("/projects/{project_id}/data-copies", status_code=202)
def build_copy(request: Request, project_id: str, body: dict[str, Any] = Body(...)):
    """Start building a "Data as of" copy at a release tag. Progress: GET the same path."""
    principal = _member(request, project_id)
    s = services_of(request)
    builder = _builder(s)
    tag = str(body.get("tag") or "")

    def effect(key: str) -> tuple[int, dict[str, Any]]:
        try:
            job = builder.start(project_id, tag)
        except CopyRefused as exc:
            raise _fail(exc) from exc
        s.audit.record(principal.role.value, "data_copy.start", job.job_id, {"project_id": project_id, "tag": tag})
        return 202, envelope(request, principal, {"project_id": project_id, **job.view()})

    return idempotent(request, principal, body, effect)


@router.get("/projects/{project_id}/data-copies")
def copy_jobs(request: Request, project_id: str) -> dict[str, Any]:
    principal = _member(request, project_id)
    s = services_of(request)
    builder = getattr(s, "data_copies", None)
    jobs = builder.list(project_id) if builder is not None else []
    return envelope(request, principal, {"project_id": project_id, "jobs": jobs})
