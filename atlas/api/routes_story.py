"""Hand-offs from the product manager and product requests.

Every mutation resolves the record first, checks the caller's project and role, then runs once per
Idempotency-Key. A record of another project answers exactly like a missing one. Each new hand-off
or request notifies the receiving role and adds a "Needs a person" entry; each answer notifies the
product manager.
"""

from __future__ import annotations

from typing import Any, Mapping

from fastapi import APIRouter, Body, Request

from atlas.api.identity import principal_of, require_role
from atlas.api.routes_runs import run_view
from atlas.api.support import ApiError, check_project, envelope, idempotent, services_of
from atlas.contracts import Principal
from atlas.projects import ANY_PROJECT
from atlas.story.store import HANDOFF_DECISIONS, HANDOFF_STATUSES, REQUEST_KINDS, REQUEST_STATUSES, StoryStore

router = APIRouter()

READERS = {"pm", "em", "architect", "audit"}
SUBJECT_TYPES = ("forecast_item", "investigation", "report_item")
HANDOFF_TARGETS = ("em", "architect")
LINK_TYPES = ("report_item", "component")
ROLE_TITLES = {"pm": "product manager", "em": "engineering manager", "architect": "solution architect"}
DECISION_TEXT = {"coding_agent": "Delegated to coding agent", "oncall": "Delegated to on-call engineer"}
KIND_TITLES = {"technical_dependency": "Technical dependency", "new_feature": "New feature"}


# ------------------------------------------------------------------ setup

def install(services: Any) -> None:
    """Give the server a store for hand-offs and requests (in memory when none was built)."""
    if getattr(services, "story", None) is None:
        services.story = StoryStore()


def _store(request: Request) -> StoryStore:
    return services_of(request).story


def _role(principal: Principal) -> str:
    if principal.role is None:
        raise ApiError(403, "forbidden", "sign-in required")
    return principal.role.value


def _reader(principal: Principal) -> str:
    role = _role(principal)
    if role not in READERS:
        raise ApiError(403, "forbidden", f"the {role} role does not take part in hand-offs and requests")
    return role


def _scope(principal: Principal) -> str | None:
    return None if principal.project_id == ANY_PROJECT else (principal.project_id or "")


def _project_for(request: Request, principal: Principal, run: Mapping[str, Any] | None = None) -> str:
    if principal.project_id not in (None, "", ANY_PROJECT):
        return str(principal.project_id)
    if run and run.get("project_id"):
        return str(run["project_id"])
    return str(services_of(request).default_project_id or "")


def _own(principal: Principal, row: Mapping[str, Any] | None, what: str) -> Mapping[str, Any]:
    if row is None:
        raise ApiError(404, "not_found", f"{what} not found")
    if principal.project_id != ANY_PROJECT and (row.get("project_id") or "") != (principal.project_id or ""):
        raise ApiError(404, "not_found", f"{what} not found")
    return row


def _line(body: Mapping[str, Any], name: str, limit: int, *, required: bool = True) -> str:
    value = " ".join(str(body.get(name) or "").split())
    if required and not value:
        raise ApiError(400, f"missing_{name}", f"{name} is required")
    if len(value) > limit:
        raise ApiError(400, f"long_{name}", f"{name} must be at most {limit} characters")
    return value


def _notify(request: Request, event: str, subject: str, project_id: str, text: str, link: str,
            roles: list[str] | None = None) -> None:
    s = services_of(request)
    try:
        s.notifier.emit(event, subject, [subject], frozenset({"internal"}),
                        {"text": text, "link": link, "required_scopes": ["internal"]}, project_id=project_id,
                        roles=roles)
    except Exception:  # a notification fault must not undo a recorded action
        pass


def _queue(request: Request, trigger: str, subject: str, project_id: str, reason: str, evidence: Mapping[str, Any]) -> None:
    try:
        services_of(request).queue.add(trigger, subject, None, reason, [dict(evidence)], ("internal",), project_id)
    except Exception:
        pass


def _view(row: Mapping[str, Any]) -> dict[str, Any]:
    return {**dict(row), "project_id": row.get("project_id") or None, "required_scopes": ["internal"]}


# ------------------------------------------------------------------ hand-offs

@router.get("/handoffs")
def handoffs(request: Request) -> dict[str, Any]:
    principal = principal_of(request)
    _reader(principal)
    rows = _store(request).handoffs(_scope(principal))
    return envelope(request, principal, {"handoffs": [_view(r) for r in rows]})


@router.get("/handoffs/{handoff_id}")
def handoff(request: Request, handoff_id: str) -> dict[str, Any]:
    principal = principal_of(request)
    _reader(principal)
    row = _own(principal, _store(request).handoff(handoff_id), "hand-off")
    return envelope(request, principal, {"handoff": _view(row)})


@router.post("/handoffs", status_code=201)
def create_handoff(request: Request, body: dict[str, Any] = Body(...)):
    principal = principal_of(request)
    require_role(principal, {"pm"}, "hand over work")
    subject_type = body.get("subject_type")
    if subject_type not in SUBJECT_TYPES:
        raise ApiError(400, "invalid_subject_type", "subject_type must be forecast_item, investigation or report_item")
    to_role = body.get("to_role")
    if to_role not in HANDOFF_TARGETS:
        raise ApiError(400, "invalid_to_role", "to_role must be em or architect")
    title = _line(body, "subject_title", 300)
    subject_id = _line(body, "subject_id", 200)
    note = _line(body, "note", 500)
    run_id = str(body.get("run_id") or "") or None
    if subject_type == "investigation" and not run_id:
        raise ApiError(400, "missing_run_id", "an investigation hand-off needs its run_id")
    run = check_project(request, principal, run_view(request, run_id), "run") if run_id else None
    project_id = _project_for(request, principal, run)
    report_id = str(body.get("report_id") or "")[:200] or None

    def effect(key: str) -> tuple[int, dict[str, Any]]:
        row = _store(request).add_handoff(project_id=project_id, subject_type=subject_type, subject_id=subject_id,
                                          subject_title=title, run_id=run_id, report_id=report_id, from_role="pm",
                                          from_user=principal.user, to_role=to_role, note=note)
        hid = row["handoff_id"]
        services_of(request).audit.record("pm", "handoff.create", hid, body)
        link = f"/handoffs/{hid}"
        _notify(request, "handoff.created", hid, project_id, f"Hand-off from the product manager: {title}", link,
                [to_role])
        _queue(request, "handoff", hid, project_id, f"Hand-off for the {ROLE_TITLES[to_role]}: {title}",
               {"for_role": to_role, "note": note, "link": link, "required_scopes": ["internal"]})
        return 201, envelope(request, principal, {"handoff": _view(row), "handoff_id": hid})

    return idempotent(request, principal, body, effect)


@router.post("/handoffs/{handoff_id}/answer")
def answer_handoff(request: Request, handoff_id: str, body: dict[str, Any] = Body(...)):
    principal = principal_of(request)
    role = _role(principal)
    row = _own(principal, _store(request).handoff(handoff_id), "hand-off")
    if role != row["to_role"]:
        raise ApiError(403, "forbidden", f"only the {ROLE_TITLES.get(row['to_role'], row['to_role'])} answers this hand-off")
    status = body.get("status")
    if status not in HANDOFF_STATUSES or status == "waiting":
        raise ApiError(400, "invalid_status", "status must be accepted, done or declined")
    reply = _line(body, "reply", 200, required=False)
    decision = body.get("decision")
    if decision is not None and (decision not in HANDOFF_DECISIONS or role != "em" or status != "accepted"):
        raise ApiError(400, "invalid_decision",
                       "only the engineering manager delegates, to coding_agent or oncall, when accepting")
    fields = {"decision": decision} if decision else {}

    def effect(key: str) -> tuple[int, dict[str, Any]]:
        out = _store(request).set_handoff(handoff_id, status=status, reply=reply or None, **fields)
        services_of(request).audit.record(role, f"handoff.{status}", handoff_id, body)
        _notify(request, "handoff.answered", f"{handoff_id}:{status}", out["project_id"],
                (DECISION_TEXT[decision] if decision else f"Hand-off {status.replace('_', ' ')}") + f": {out['subject_title']}"
                + (f" ({reply})" if reply else ""),
                f"/handoffs/{handoff_id}", ["pm"])
        return 200, envelope(request, principal, {"handoff": _view(out)})

    return idempotent(request, principal, body, effect)


# ------------------------------------------------------------------ product requests

@router.get("/requests")
def requests(request: Request) -> dict[str, Any]:
    principal = principal_of(request)
    _reader(principal)
    rows = _store(request).requests(_scope(principal))
    return envelope(request, principal, {"requests": [_view(r) for r in rows]})


@router.get("/requests/{request_id}")
def product_request(request: Request, request_id: str) -> dict[str, Any]:
    principal = principal_of(request)
    _reader(principal)
    row = _own(principal, _store(request).request(request_id), "request")
    return envelope(request, principal, {"request": _view(row)})


@router.post("/requests", status_code=201)
def create_request(request: Request, body: dict[str, Any] = Body(...)):
    principal = principal_of(request)
    require_role(principal, {"pm"}, "file a request")
    kind = body.get("kind")
    if kind not in REQUEST_KINDS:
        raise ApiError(400, "invalid_kind", "kind must be technical_dependency or new_feature")
    title = _line(body, "title", 200)
    description = _line(body, "description", 1000)
    link_type = body.get("link_type") or None
    if link_type is not None and link_type not in LINK_TYPES:
        raise ApiError(400, "invalid_link_type", "link_type must be report_item or component")
    link_id = _line(body, "link_id", 200, required=False) or None
    link_label = _line(body, "link_label", 200, required=False) or None
    if link_type and not link_id:
        raise ApiError(400, "missing_link_id", "a link needs its link_id")
    project_id = _project_for(request, principal)

    def effect(key: str) -> tuple[int, dict[str, Any]]:
        row = _store(request).add_request(project_id=project_id, kind=kind, title=title, description=description,
                                          link_type=link_type, link_id=link_id if link_type else None,
                                          link_label=link_label if link_type else None, from_role="pm",
                                          from_user=principal.user)
        rid = row["request_id"]
        services_of(request).audit.record("pm", "request.create", rid, body)
        link = f"/requests?focus={rid}"
        _notify(request, "request.created", rid, project_id, f"{KIND_TITLES[kind]} request: {title}", link,
                [row["owner_role"]])
        _notify(request, "request.shared", rid, project_id, f"For your information: {title}", link, [row["info_role"]])
        _queue(request, "product_request", rid, project_id,
               f"{KIND_TITLES[kind]} request for the {ROLE_TITLES[row['owner_role']]}: {title}",
               {"for_role": row["owner_role"], "link": link, "required_scopes": ["internal"]})
        return 201, envelope(request, principal, {"request": _view(row), "request_id": rid})

    return idempotent(request, principal, body, effect)


@router.post("/requests/{request_id}/answer")
def answer_request(request: Request, request_id: str, body: dict[str, Any] = Body(...)):
    principal = principal_of(request)
    role = _role(principal)
    row = _own(principal, _store(request).request(request_id), "request")
    if role != row["owner_role"]:
        raise ApiError(403, "forbidden", f"only the {ROLE_TITLES[row['owner_role']]} answers this request")
    status = body.get("status")
    if status not in REQUEST_STATUSES or status == "new":
        raise ApiError(400, "invalid_status", "status must be accepted, needs_more_info or declined")
    reply = _line(body, "reply", 200)

    def effect(key: str) -> tuple[int, dict[str, Any]]:
        out = _store(request).answer_request(request_id, status, reply, role)
        services_of(request).audit.record(role, f"request.{status}", request_id, body)
        _notify(request, "request.answered", f"{request_id}:{status}", out["project_id"],
                f"{out['title']}: {status.replace('_', ' ')}. {reply}", f"/requests?focus={request_id}")
        return 200, envelope(request, principal, {"request": _view(out)})

    return idempotent(request, principal, body, effect)


# ------------------------------------------------------------------ home figures

@router.get("/story/home")
def home(request: Request) -> dict[str, Any]:
    """The hand-off, request and agent figures for the caller's home page."""
    principal = principal_of(request)
    role = _role(principal)
    store = _store(request)
    scope = _scope(principal)
    out: dict[str, Any] = {"role": role}
    if role in ("em", "architect"):
        waiting = [_view(h) for h in store.handoffs(scope) if h["to_role"] == role and h["status"] == "waiting"]
        mine = [r for r in store.requests(scope) if role in (r["owner_role"], r["info_role"])]
        out["handoffs_waiting"] = len(waiting)
        out["handoffs"] = waiting[:5]
        out["requests"] = {
            "new": sum(1 for r in mine if r["status"] == "new"),
            "owned": sum(1 for r in mine if r["owner_role"] == role and r["status"] in ("new", "needs_more_info")),
            "for_information": sum(1 for r in mine if r["info_role"] == role),
            "items": [{**_view(r), "for_information": r["info_role"] == role} for r in mine[:5]],
        }
    if role == "pm":
        mine = [r for r in store.requests(scope) if principal.user is None or r["from_user"] in (None, principal.user)]
        out["my_requests"] = {"total": len(mine), "open": sum(1 for r in mine if r["status"] in ("new", "needs_more_info")),
                              "items": [_view(r) for r in mine[:5]]}
        sent = [h for h in store.handoffs(scope) if principal.user is None or h["from_user"] in (None, principal.user)]
        out["handoffs_sent"] = {"total": len(sent), "waiting": sum(1 for h in sent if h["status"] == "waiting"),
                                "items": [_view(h) for h in sent[:5]]}
    return envelope(request, principal, {**out, "required_scopes": ["internal"]})
