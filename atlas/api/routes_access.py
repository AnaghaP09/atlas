"""Locked items, related items and access requests.

A person who meets an item their role does not read sees it locked and can ask for it. Ordinary
items are granted at once (read-only, that one item, 24 hours); sensitive items wait for the role
that owns them. No grant ever allows a change.
"""

from __future__ import annotations

from typing import Any, Mapping

from fastapi import APIRouter, Body, Query, Request

from atlas.access import GRANTED, PENDING, AccessError, KINDS
from atlas.api import access_control as ac
from atlas.api.identity import principal_of
from atlas.api.ports import NotWired
from atlas.api.support import ApiError, envelope, idempotent, services_of
from atlas.contracts import Principal
from atlas.projects import ANY_PROJECT

router = APIRouter()

REQUESTABLE_TYPES = sorted({k.split(":")[0] for k in KINDS})


def _role(principal: Principal) -> str:
    if principal.role is None:
        raise ApiError(403, "forbidden", "sign-in required")
    return principal.role.value


def _project_of(principal: Principal, item: ac.Item | None = None) -> str:
    if item is not None and item.project_id:
        return item.project_id
    return "" if principal.project_id in (None, ANY_PROJECT) else str(principal.project_id)


def request_view(req: Mapping[str, Any]) -> dict[str, Any]:
    """What the API shows of a request. Carries the project so other projects never see it."""
    return {
        "request_id": req["request_id"], "project_id": req["project_id"] or None,
        "requester": {"user": req["requester_user"], "role": req["requester_role"]},
        "object_type": req["object_type"], "object_id": req["object_id"], "label": req["label"],
        "owner_role": req["owner_role"], "needs_decision": req["sensitive"], "reason": req["reason"],
        "created_at": req["created_at"], "status": req["status"], "grant": req["grant"],
        "required_scopes": ["internal"],
    }


def _own_request(request: Request, principal: Principal, request_id: str) -> dict[str, Any]:
    """A request in the caller's project, or 404 (another project's request looks missing)."""
    req = services_of(request).access.get(request_id)
    if req is None:
        raise ApiError(404, "not_found", "request not found")
    if principal.project_id != ANY_PROJECT and (req["project_id"] or None) != (principal.project_id or None):
        raise ApiError(404, "not_found", "request not found")
    return req


def _notify(request: Request, event: str, req: Mapping[str, Any], roles: list[str], text: str) -> None:
    s = services_of(request)
    try:
        s.notifier.emit(event, req["request_id"], [req["request_id"]], frozenset({"internal"}),
                        {"text": text, "request_id": req["request_id"], "object_type": req["object_type"],
                         "label": req["label"], "required_scopes": ["internal"]},
                        project_id=req["project_id"] or "", roles=roles)
    except Exception:  # a notification fault must not undo a recorded decision
        pass


def _wrap(exc: AccessError) -> ApiError:
    return ApiError(exc.status, exc.error, exc.reason)


# ------------------------------------------------------------------ related items and single reads

def _related_ids(request: Request, item: ac.Item) -> list[tuple[str, str]]:
    """Items linked to ``item`` in the stored data. Only real relations are listed."""
    from atlas.api.routes_runs import run_view

    s = services_of(request)
    obj = item.obj
    out: list[tuple[str, str]] = []

    def for_run(run_id: str, skip_report: str | None = None) -> None:
        view = run_view(request, run_id)
        if not view:
            return
        for r in view.get("reports", []):
            if r["report_id"] != skip_report:
                out.append(("report", r["report_id"]))
        if view.get("snapshot_id"):
            out.append(("graph", str(view["snapshot_id"])))
        try:
            for t in s.tasks.tasks_for_run(run_id):
                out.append(("task", str(t["task_id"])))
        except NotWired:
            pass
        auth = s.orchestrator.runs.authoritative(run_id) if s.orchestrator is not None else None
        if auth:
            for n, f in enumerate(auth[1].get("findings") or []):
                if "restricted:security" in set(f.get("required_scopes") or []):
                    out.append(("finding", f"{run_id}:{n}"))

    if item.object_type == "run":
        for_run(item.object_id)
    elif item.object_type == "report":
        out.append(("run", obj["run_id"]))
        for_run(obj["run_id"], skip_report=item.object_id)
    elif item.object_type == "finding":
        out.append(("run", obj["run_id"]))
    elif item.object_type == "task":
        if obj.get("run_id"):
            out.append(("run", str(obj["run_id"])))
            view = run_view(request, str(obj["run_id"]))
            for r in (view or {}).get("reports", []):
                out.append(("report", r["report_id"]))
        try:
            for a in s.tasks.approvals(project_id=item.project_id):
                if a.get("task_id") == item.object_id:
                    out.append(("approval", str(a["approval_id"])))
        except NotWired:
            pass
    elif item.object_type == "approval":
        if obj.get("task_id"):
            out.append(("task", str(obj["task_id"])))
            try:
                t = s.tasks.get(str(obj["task_id"]))
            except NotWired:
                t = None
            if t and t.get("run_id"):
                out.append(("run", str(t["run_id"])))
    elif item.object_type == "kill_switch":
        for inc in obj.get("incidents", []) or []:
            if inc.get("incident_id"):
                out.append(("incident", str(inc["incident_id"])))
    seen: set[tuple[str, str]] = set()
    unique = []
    for pair in out:
        if pair not in seen and pair != (item.object_type, item.object_id):
            seen.add(pair)
            unique.append(pair)
    return unique


@router.get("/related/{object_type}/{object_id:path}")
def related(request: Request, object_type: str, object_id: str) -> dict[str, Any]:
    """Items linked to this one, each marked open or locked for the caller.

    A locked item shows only its type and a neutral label, never a title or content."""
    principal = principal_of(request)
    item = ac.resolve(request, principal, object_type, object_id)
    this = ac.access_of(request, principal, object_type, object_id)
    items = [e for e in (ac.access_of(request, principal, t, i) for t, i in _related_ids(request, item))
             if e is not None]
    return envelope(request, principal, {"object_type": object_type, "object_id": object_id,
                                         "access": this["access"] if this else ac.LOCKED, "related": items})


def _content(request: Request, item: ac.Item, reader: Principal) -> tuple[dict[str, Any], str]:
    """The item's content for a reader already authorised for it."""
    from atlas.api.routes_runs import _report
    from atlas.api.routes_tasks import task_view

    s = services_of(request)
    if item.object_type == "task":
        return {"task": task_view(item.obj)}, "REAL"
    if item.object_type == "approval":
        return {"approval": dict(item.obj, required_scopes=item.scopes)}, "REAL"
    if item.object_type == "report":
        body = _report(request, reader, item.object_id, None)
        return body, body.get("data_mode", "REAL")
    if item.object_type == "finding":
        return {"finding": dict(item.obj, required_scopes=item.scopes)}, "REAL"
    if item.object_type == "run":
        return {"run": item.obj}, "REAL"
    if item.object_type == "graph":
        return {"snapshot_id": item.object_id, **dict(s.snapshots.graph(item.object_id))}, "REAL"
    if item.object_type == "kill_switch":
        return {"switch": item.obj}, "REAL"
    if item.object_type == "incident":
        return {"incident": item.obj}, "REAL"
    return {"deliveries": [dict(d, required_scopes=["internal"]) for d in s.notifier.deliveries("dead_letter")]}, "REAL"


@router.get("/objects/{object_type}/{object_id:path}")
def read_object(request: Request, object_type: str, object_id: str) -> dict[str, Any]:
    """Read one item if it is open to the caller's role or granted to the caller; else 403 locked."""
    principal = principal_of(request)
    item, reader, grant = ac.reader_for(request, principal, object_type, object_id)
    content, mode = _content(request, item, reader)
    meta = {"object_type": object_type, "object_id": object_id, "label": item.policy.label,
            "access": ac.OPEN, "via_grant": grant is not None, "read_only": grant is not None,
            "grant_expires_at": grant["expires_at"] if grant else None}
    if item.object_type == "report":  # already an enveloped, projected body
        return {**content, **meta}
    return envelope(request, reader, {**content, **meta}, data_mode=mode)


# ------------------------------------------------------------------ requests

@router.post("/access-requests", status_code=201)
def create_request(request: Request, body: dict[str, Any] = Body(...)):
    principal = principal_of(request)
    role = _role(principal)
    object_type, object_id = str(body.get("object_type", "")), str(body.get("object_id", ""))
    if object_type not in REQUESTABLE_TYPES or not object_id:
        raise ApiError(400, "invalid_request", "object_type and object_id are required")
    reason = body.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise ApiError(400, "invalid_request", "reason must be text")
    item = ac.resolve(request, principal, object_type, object_id)  # 404 for missing or another project
    if ac.readable_without_grant(principal, item):
        raise ApiError(409, "already_open", "this item is already open to your role")
    if not item.policy.grantable:
        raise ApiError(409, "not_requestable", "access to this item cannot be requested")
    s = services_of(request)

    def effect(key: str) -> tuple[int, dict[str, Any]]:
        try:
            req, created = s.access.create(
                project_id=_project_of(principal, item), user=principal.user, role=role, object_type=object_type,
                object_id=object_id, label=item.policy.label, owner_role=item.policy.owner,
                sensitive=item.policy.sensitive, reason=reason)
            if created:
                s.audit.record(role, "access.request", req["request_id"],
                               {"object_type": object_type, "object_id": object_id, "user": principal.user})
                if item.policy.sensitive:
                    s.queue.add("access_request", req["request_id"], None,
                                f"access request for a {item.policy.label.lower()}",
                                [{"request_id": req["request_id"], "owner_role": item.policy.owner}],
                                project_id=req["project_id"])
                    _notify(request, "access.requested", req, [item.policy.owner],
                            f"{role} asks to read a {item.policy.label.lower()}")
                else:
                    req = s.access.grant(req["request_id"], by_user=None, by_role="automatic", automatic=True)
                    s.audit.record("automatic", "access.grant", req["request_id"],
                                   {"hours": 24, "object_type": object_type, "object_id": object_id})
                    _notify(request, "access.granted_automatically", req, [item.policy.owner],
                            f"{role} was given read-only access to a {item.policy.label.lower()} for 24 hours")
        except AccessError as exc:
            raise _wrap(exc) from exc
        access = ac.OPEN if req["status"] == GRANTED else ac.LOCKED
        return (201 if created else 200), envelope(request, principal,
                                                   {"request": request_view(req), "access": access})

    return idempotent(request, principal, body, effect)


@router.get("/access-requests")
def list_requests(request: Request, box: str = Query("mine")) -> dict[str, Any]:
    """``box=mine``: the caller's own requests. ``box=to_decide``: requests and live grants for the
    items the caller's role owns."""
    principal = principal_of(request)
    role = _role(principal)
    s = services_of(request)
    if box not in ("mine", "to_decide"):
        raise ApiError(400, "invalid_box", "box must be mine or to_decide")
    project = _project_of(principal)
    items = s.access.mine(ac.who(principal), project) if box == "mine" else s.access.to_decide(role, project)
    return envelope(request, principal, {"box": box, "requests": [request_view(r) for r in items]})


@router.post("/access-requests/{request_id}/decision")
def decide_request(request: Request, request_id: str, body: dict[str, Any] = Body(...)):
    principal = principal_of(request)
    role = _role(principal)
    req = _own_request(request, principal, request_id)
    decision = body.get("decision")
    if decision not in ("grant", "deny"):
        raise ApiError(400, "invalid_decision", "decision must be grant or deny")
    if req["requester"] == ac.who(principal):
        raise ApiError(403, "forbidden", "you cannot decide your own request")
    if role != req["owner_role"]:
        raise ApiError(403, "forbidden", f"only the {req['owner_role']} role decides this request")
    hours = body.get("hours")
    if hours is not None and (isinstance(hours, bool) or not isinstance(hours, (int, float))):
        raise ApiError(400, "invalid_hours", "hours must be a number")
    if decision == "grant":
        # The approver must be able to read the item themselves, and it must still be grantable.
        item = ac.resolve(request, principal, req["object_type"], req["object_id"])
        if not item.policy.grantable or item.policy.owner != role or not ac.readable_without_grant(principal, item):
            raise ApiError(403, "forbidden", "you cannot grant access to an item you cannot read and do not own")
    s = services_of(request)

    def effect(key: str) -> tuple[int, dict[str, Any]]:
        try:
            if decision == "grant":
                out = s.access.grant(request_id, by_user=principal.user, by_role=role, hours=hours)
            else:
                out = s.access.deny(request_id, by_user=principal.user, by_role=role)
        except AccessError as exc:
            raise _wrap(exc) from exc
        s.audit.record(role, f"access.{decision}", request_id, {"hours": hours, "user": principal.user})
        _notify(request, "access.decided", out, [out["requester_role"]],
                f"your request to read a {out['label'].lower()} was {'granted' if decision == 'grant' else 'denied'}")
        return 200, envelope(request, principal, {"request": request_view(out)})

    return idempotent(request, principal, body, effect)


@router.post("/access-requests/{request_id}/withdraw")
def withdraw_request(request: Request, request_id: str, body: dict[str, Any] | None = Body(None)):
    principal = principal_of(request)
    role = _role(principal)
    req = _own_request(request, principal, request_id)
    if req["requester"] != ac.who(principal):
        raise ApiError(403, "forbidden", "only the person who asked can withdraw a request")
    s = services_of(request)

    def effect(key: str) -> tuple[int, dict[str, Any]]:
        try:
            out = s.access.withdraw(request_id, by_user=principal.user, by_role=role)
        except AccessError as exc:
            raise _wrap(exc) from exc
        s.audit.record(role, "access.withdraw", request_id, {"user": principal.user})
        return 200, envelope(request, principal, {"request": request_view(out)})

    return idempotent(request, principal, body or {}, effect)


@router.post("/access-grants/{grant_id}/revoke")
def revoke_grant(request: Request, grant_id: str, body: dict[str, Any] | None = Body(None)):
    principal = principal_of(request)
    role = _role(principal)
    s = services_of(request)
    req = s.access.by_grant(grant_id)
    if req is None:
        raise ApiError(404, "not_found", "grant not found")
    req = _own_request(request, principal, req["request_id"])
    if role not in (req["owner_role"], "audit"):
        raise ApiError(403, "forbidden", f"only the {req['owner_role']} role or audit can revoke this grant")

    def effect(key: str) -> tuple[int, dict[str, Any]]:
        try:
            out = s.access.revoke(req["request_id"], by_user=principal.user, by_role=role)
        except AccessError as exc:
            raise _wrap(exc) from exc
        s.audit.record(role, "access.revoke", req["request_id"], {"grant_id": grant_id, "user": principal.user})
        _notify(request, "access.revoked", out, [out["requester_role"]],
                f"your access to a {out['label'].lower()} was withdrawn")
        return 200, envelope(request, principal, {"request": request_view(out)})

    return idempotent(request, principal, body or {}, effect)


def sweep_expiring(services: Any) -> int:
    """Remind requesters whose grants end within the hour. Run by the housekeeper."""
    sent = 0
    for req in services.access.expiring():
        try:
            services.notifier.emit("access.expiring", req["request_id"], [req["request_id"]],
                                   frozenset({"internal"}),
                                   {"text": f"your access to a {req['label'].lower()} ends soon",
                                    "request_id": req["request_id"], "required_scopes": ["internal"]},
                                   project_id=req["project_id"] or "", roles=[req["requester_role"]])
            sent += 1
        except Exception:
            pass
    return sent


__all__ = ["PENDING", "request_view", "router", "sweep_expiring"]
