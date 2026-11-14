"""Tasks, approvals, execution steps, security refresh and the scorecard.

Every mutation resolves the task or approval first, checks the caller's project and role, and only
then calls the execution layer, which applies the eligibility rules.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Request

from atlas.api import access_control as ac
from atlas.api.identity import principal_of, require_role
from atlas.api.ports import Refused
from atlas.api.routes_runs import run_view
from atlas.api.support import ApiError, check_project, envelope, idempotent, services_of
from atlas.projects import ANY_PROJECT

router = APIRouter()

POST_PUBLICATION_FIELDS = ("ci_observations", "behavioural_check", "adjudication", "pull_request")


def task_view(raw: Any) -> dict[str, Any]:
    """Explicit nulls for post-publication fields until a publication exists."""
    view = dict(raw)
    for name in POST_PUBLICATION_FIELDS:
        view.setdefault(name, None)
    view.setdefault("required_scopes", ["internal"])
    return view


def _scope(principal: Any) -> str | None:
    """The project filter for list reads (None: a demo caller not limited to one project)."""
    return None if principal.project_id == ANY_PROJECT else principal.project_id


def _readable(request: Request, principal: Any, object_type: str, id_field: str,
              rows: Any) -> tuple[list[Any], list[dict[str, Any]]]:
    """Split a list into the rows the caller may read and locked placeholders for the rest.

    A locked placeholder carries the type, id and neutral label only, so the caller can ask for it."""
    shown: list[Any] = []
    locked: list[dict[str, Any]] = []
    for row in rows:
        entry = ac.access_of(request, principal, object_type, str(row[id_field]))
        if entry is None:
            continue
        if entry["access"] == ac.OPEN:
            shown.append(row)
        else:
            locked.append(entry)
    return shown, locked


def _refusal(exc: Refused) -> ApiError:
    return ApiError(exc.status, "refused", exc.reason)


@router.post("/tasks", status_code=201)
def create_task(request: Request, body: dict[str, Any] = Body(...)):
    principal = principal_of(request)
    require_role(principal, {"em"}, "create a task")
    s = services_of(request)
    run_id = str(body.get("run_id", ""))
    check_project(request, principal, run_view(request, run_id), "run")
    auth = s.orchestrator.runs.authoritative(run_id) if s.orchestrator is not None else None
    if auth is None:
        raise ApiError(409, "not_verified", "the run has no verified result")
    gen, artefact = auth
    action = (artefact.get("action") or {})
    if action.get("value") != "DELEGATE_TO_CODING_AGENT":
        raise ApiError(409, "not_delegable", "the authoritative verified result does not delegate a task")
    if s.mode.get().value == "offline":
        raise ApiError(403, "offline", "the initial security scan cannot run in Offline mode")

    def effect(key: str) -> tuple[int, dict[str, Any]]:
        try:
            task = s.tasks.create(run_id, gen, action, principal.role.value, key)
        except Refused as exc:
            raise _refusal(exc) from exc
        s.audit.record(principal.role.value, "task.create", str(task.get("task_id")), body)
        return 201, envelope(request, principal, {"task": task_view(task)})

    return idempotent(request, principal, body, effect)


@router.get("/tasks")
def tasks(request: Request) -> dict[str, Any]:
    principal = principal_of(request)
    items = services_of(request).tasks.list(project_id=_scope(principal))
    shown, locked = _readable(request, principal, "task", "task_id", items)
    return envelope(request, principal, {"tasks": [task_view(t) for t in shown], "locked_items": locked})


@router.get("/tasks/{task_id}")
def task(request: Request, task_id: str) -> dict[str, Any]:
    principal = principal_of(request)
    item, reader, grant = ac.reader_for(request, principal, "task", task_id)
    return envelope(request, reader, {"task": task_view(item.obj), "read_only": grant is not None})


@router.get("/approvals")
def approvals(request: Request) -> dict[str, Any]:
    principal = principal_of(request)
    rows = services_of(request).tasks.approvals(project_id=_scope(principal))
    shown, locked = _readable(request, principal, "approval", "approval_id", rows)
    items = [dict(a, required_scopes=a.get("required_scopes", ["internal"])) for a in shown]
    return envelope(request, principal, {"approvals": items, "locked_items": locked})


@router.get("/approvals/{approval_id}")
def approval(request: Request, approval_id: str) -> dict[str, Any]:
    principal = principal_of(request)
    item, reader, grant = ac.reader_for(request, principal, "approval", approval_id)
    return envelope(request, reader, {"approval": dict(item.obj, required_scopes=item.scopes),
                                      "read_only": grant is not None})


@router.post("/approvals/{approval_id}/decision")
def decide(request: Request, approval_id: str, body: dict[str, Any] = Body(...)):
    principal = principal_of(request)
    require_role(principal, {"em"}, "decide approvals")
    s = services_of(request)
    check_project(request, principal, s.tasks.approval(approval_id), "approval")
    decision = body.get("decision")
    if decision not in {"approve", "reject"}:
        raise ApiError(400, "invalid_decision", "decision must be approve or reject")
    try:
        minutes = float(body.get("review_minutes", 0))
    except (TypeError, ValueError) as exc:
        raise ApiError(400, "invalid_review_minutes", "review_minutes must be a number") from exc

    def effect(key: str) -> tuple[int, dict[str, Any]]:
        try:
            out = s.tasks.decide(approval_id, decision, minutes, principal.role.value, key)
        except Refused as exc:
            raise _refusal(exc) from exc
        s.audit.record(principal.role.value, f"approval.{decision}", approval_id, body)
        return 200, envelope(request, principal, {"approval": dict(out, required_scopes=["internal"])})

    return idempotent(request, principal, body, effect)


def _task_step(request: Request, task_id: str, step: str, roles: set[str], body: dict[str, Any] | None):
    principal = principal_of(request)
    require_role(principal, roles, step.replace("_", " ") + " a task")
    s = services_of(request)
    check_project(request, principal, s.tasks.get(task_id), "task")

    def effect(key: str) -> tuple[int, dict[str, Any]]:
        try:
            if step == "adjudicate":
                verdict = (body or {}).get("verdict")
                if verdict not in {"correct", "incorrect"}:
                    raise ApiError(400, "invalid_verdict", "verdict must be correct or incorrect")
                out = s.tasks.adjudicate(task_id, verdict, str((body or {}).get("note", "")), principal.role.value, key)
            else:
                out = getattr(s.tasks, step)(task_id, principal.role.value, key)
        except Refused as exc:
            raise _refusal(exc) from exc
        s.audit.record(principal.role.value, f"task.{step}", task_id, body or {})
        return 200, envelope(request, principal, {"task": task_view(out)})

    return idempotent(request, principal, body or {}, effect)


@router.post("/tasks/{task_id}/prepare")
def prepare(request: Request, task_id: str, body: dict[str, Any] | None = Body(None)):
    return _task_step(request, task_id, "prepare", {"em"}, body)


@router.post("/tasks/{task_id}/publish")
def publish(request: Request, task_id: str, body: dict[str, Any] | None = Body(None)):
    return _task_step(request, task_id, "publish", {"em"}, body)


@router.post("/tasks/{task_id}/adjudicate")
def adjudicate(request: Request, task_id: str, body: dict[str, Any] = Body(...)):
    return _task_step(request, task_id, "adjudicate", {"em", "architect", "audit"}, body)


@router.post("/tasks/{task_id}/security-refresh")
def security_refresh(request: Request, task_id: str, body: dict[str, Any] | None = Body(None)):
    return _task_step(request, task_id, "security_refresh", {"em"}, body)


@router.get("/scorecard")
def scorecard(request: Request) -> dict[str, Any]:
    principal = principal_of(request)
    card = dict(services_of(request).tasks.scorecard(project_id=_scope(principal)))
    return envelope(request, principal, card, data_mode=card.get("data_mode", "SYNTHETIC"))
