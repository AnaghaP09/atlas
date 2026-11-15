"""Streams, notifications, destinations, mode, containment, corrections and health."""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

from fastapi import APIRouter, Body, Header, Query, Request
from fastapi.responses import StreamingResponse

from atlas.api.identity import principal_of, require_role
from atlas.api.ports import NotWired, Refused
from atlas.api.routes_runs import run_view
from atlas.api.streams import KEEPALIVE, NOTIFICATION_STREAM, RUN_STREAM, encode
from atlas.api.support import ApiError, check_project, envelope, idempotent, services_of, staged
from atlas.contracts import Mode

router = APIRouter()

KEEPALIVE_SECONDS = 15.0
POLL_SECONDS = 0.25
SESSION_RECHECK_SECONDS = 15.0


# ------------------------------------------------------------------ streams

def _bind_session(request: Request) -> Any:
    """In sign-in mode, bind the stream to the caller's session. Returns a handle or None.

    The handle closes when the session signs out or expires; the stream then stops. Checking the
    handle never extends the session's idle time."""
    identity = getattr(request.app.state, "identity", None)
    provider = getattr(identity, "provider", None)
    sessions = getattr(request.app.state, "sessions", None)
    if provider is None or sessions is None:
        return None
    session = provider.session_of(request, touch=False)
    if session is None:
        raise ApiError(401, "unauthorised", "sign-in required")
    return sessions.bind_stream(session)


async def _stream(request: Request, stream: str, last_event_id: str | None, run_id: str | None,
                  follow: bool, handle: Any = None, project_id: str | None = None) -> AsyncIterator[str]:
    principal = principal_of(request)

    def alive() -> bool:
        return handle is None or (not handle.closed and handle.still_valid())

    hub = services_of(request).hub
    try:
        if not alive():
            return
        reset, events = hub.read(stream, principal, last_event_id, run_id, project_id)
        if reset is not None:
            yield encode(reset)
        last = last_event_id if reset is None else None
        for e in events:
            if not alive():
                return
            yield encode(e)
            last = e["event_id"]
        if not follow:
            return
        since_keepalive = 0.0
        since_recheck = 0.0
        seen_version = hub.version
        while not await request.is_disconnected():
            await asyncio.sleep(POLL_SECONDS)
            since_keepalive += POLL_SECONDS
            since_recheck += POLL_SECONDS
            if handle is not None and (handle.closed or since_recheck >= SESSION_RECHECK_SECONDS):
                since_recheck = 0.0
                if not alive():
                    return
            if hub.version != seen_version:
                seen_version = hub.version
                r, new = hub.read(stream, principal, last, run_id, project_id)
                if r is not None:
                    if not alive():
                        return
                    yield encode(r)
                for e in new:
                    if not alive():
                        return
                    yield encode(e)
                    last = e["event_id"]
            if since_keepalive >= KEEPALIVE_SECONDS:
                since_keepalive = 0.0
                yield KEEPALIVE
    finally:
        if handle is not None and not handle.closed:
            handle.close()


def _sse(gen: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(gen, media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.get("/runs/{run_id}/stream")
def run_stream(request: Request, run_id: str, follow: bool = Query(True),
               last_event_id: str | None = Header(None)):
    principal = principal_of(request)
    check_project(request, principal, run_view(request, run_id), "run")
    return _sse(_stream(request, RUN_STREAM, last_event_id, run_id, follow, _bind_session(request)))


@router.get("/notifications/stream")
def notification_stream(request: Request, follow: bool = Query(True), last_event_id: str | None = Header(None)):
    principal_of(request)
    return _sse(_stream(request, NOTIFICATION_STREAM, last_event_id, None, follow, _bind_session(request)))


# ------------------------------------------------------------------ notifications

def _resolved(request: Request) -> set[str]:
    return {e["subject"] for e in services_of(request).audit.entries() if e["action"] == "notification.resolve"}


@router.get("/notifications")
def notifications(request: Request, severity: str | None = Query(None), kind: str | None = Query(None),
                  state: str | None = Query(None), subject_id: str | None = Query(None)) -> dict[str, Any]:
    principal = principal_of(request)
    s = services_of(request)
    resolved = _resolved(request)
    items = []
    for n in s.notifier.in_app(principal):
        n = dict(n)
        n["status"] = "resolved" if n["id"] in resolved else n.get("status", "open")
        content = n.get("content") if isinstance(n.get("content"), dict) else {}
        subject = str(n.get("subject_id") or "")
        n.update(notification_id=n["id"], state=n["status"], created_at=n.get("created"),
                 message=str(content.get("text") or n.get("event_type")),
                 acknowledged=n["status"] in ("acknowledged", "resolved"), resolved=n["status"] == "resolved",
                 run_id=subject if subject.startswith("run-") else None,
                 task_id=subject if subject.startswith("task-") else None)
        if severity and n["severity"] != severity or kind and n["kind"] != kind:
            continue
        if state and n["status"] != state or subject_id and n["subject_id"] != subject_id:
            continue
        items.append(n)
    return envelope(request, principal, {"notifications": items})


def _notification_action(request: Request, notification_id: str, action: str):
    principal = principal_of(request)
    s = services_of(request)
    rec = next((r for r in s.notifier.records() if r["id"] == notification_id), None)
    if rec is None:
        raise ApiError(404, "not_found", "notification not found")
    role = principal.role.value if principal.role else None
    if role != "audit" and role not in rec["roles"] and role not in rec["backup_roles"]:
        raise ApiError(403, "forbidden", "only a target role or audit may act on this notification")

    def effect(key: str) -> tuple[int, dict[str, Any]]:
        if action == "ack":
            s.notifier.acknowledge(notification_id)
        elif hasattr(s.notifier, "resolve"):
            s.notifier.resolve(notification_id)
        s.audit.record(role, f"notification.{action}", notification_id, {})
        clock = getattr(s, "metrics", None)
        if clock is not None:
            clock.changed(principal.project_id if principal.project_id != "*" else None, ("unread_notifications",))
        return 200, envelope(request, principal, {"notification_id": notification_id,
                                                  "status": "acknowledged" if action == "ack" else "resolved"})

    return idempotent(request, principal, {}, effect)


@router.post("/notifications/{notification_id}/ack")
def ack(request: Request, notification_id: str):
    return _notification_action(request, notification_id, "ack")


@router.post("/notifications/{notification_id}/resolve")
def resolve(request: Request, notification_id: str):
    return _notification_action(request, notification_id, "resolve")


@router.get("/notifications/dead-letter")
def dead_letter(request: Request) -> dict[str, Any]:
    principal = principal_of(request)
    require_role(principal, {"audit", "pm", "em", "architect"}, "read the dead-letter list")
    s = services_of(request)
    return envelope(request, principal, {"deliveries": s.notifier.deliveries("dead_letter")})


@router.get("/destinations")
def destinations(request: Request) -> dict[str, Any]:
    principal = principal_of(request)
    s = services_of(request)
    items = [{"name": d.name, "audience": d.audience, "declared": d.audience is not None}
             for d in getattr(s.notifier, "destinations", [])]
    return staged(request, principal, "chat delivery has no workspace in the demo; deliveries go to a local test receiver",
                  destinations=items)


@router.post("/webhooks/test")
def webhook_test(request: Request, body: dict[str, Any] | None = Body(None)):
    principal = principal_of(request)
    require_role(principal, {"audit", "em"}, "test webhooks")
    s = services_of(request)

    def effect(key: str) -> tuple[int, dict[str, Any]]:
        previews = []
        sample = {"text": "Test notification from ATLAS", "required_scopes": ["public"]}
        for d in getattr(s.notifier, "destinations", []):
            if d.audience is None:
                previews.append({"destination": d.name, "status": "held", "reason": "audience_undeclared"})
                continue
            from atlas.projection import channel_principal_for

            projected = s.projection.project({"required_scopes": [], "content": sample}, channel_principal_for(d.audience))
            previews.append({"destination": d.name, "status": "preview", "audience": d.audience,
                             "payload": projected.get("content")})
        sent = s.notifier.test_send(sample) if hasattr(s.notifier, "test_send") and s.notifier.destinations else []
        return 200, staged(request, principal, "chat delivery has no workspace in the demo", previews=previews,
                           deliveries=sent)

    return idempotent(request, principal, body or {}, effect)


# ------------------------------------------------------------------ mode

@router.get("/mode")
def get_mode(request: Request) -> dict[str, Any]:
    principal = principal_of(request)
    return envelope(request, principal, {"mode": services_of(request).mode.get().value})


@router.post("/mode")
def set_mode(request: Request, body: dict[str, Any] = Body(...)):
    principal = principal_of(request)
    require_role(principal, {"audit"}, "change the mode")
    s = services_of(request)
    value = str(body.get("mode", "")).lower()
    if value not in {"connected", "offline"}:
        raise ApiError(400, "invalid_mode", "mode must be connected or offline")

    def effect(key: str) -> tuple[int, dict[str, Any]]:
        s.mode.set(Mode(value))
        if value == "connected":
            resume = getattr(s.notifier, "resume_paused", None)
            if resume:
                resume()
        s.audit.record(principal.role.value, "mode.set", value, body)
        return 200, envelope(request, principal, {"mode": value})

    return idempotent(request, principal, body, effect)


# ------------------------------------------------------------------ containment and corrections

def _port_call(fn: Any, *args: Any) -> Any:
    try:
        return fn(*args)
    except Refused as exc:
        raise ApiError(exc.status, "refused", exc.reason) from exc


@router.get("/containment")
def containment(request: Request) -> dict[str, Any]:
    principal = principal_of(request)
    return envelope(request, principal, dict(services_of(request).containment.state()))


@router.get("/containment/{switch_id}")
def containment_switch(request: Request, switch_id: str) -> dict[str, Any]:
    principal = principal_of(request)
    sw = services_of(request).containment.switch(switch_id)
    if sw is None:
        raise ApiError(404, "not_found", "switch not found")
    return envelope(request, principal, {"switch": dict(sw)})


@router.post("/containment/{switch_id}/drill", status_code=201)
def drill(request: Request, switch_id: str, body: dict[str, Any] | None = Body(None)):
    """Trip one kill switch as a drill. It really blocks what the switch blocks until the audit
    role ends it with ``POST /api/incidents/{id}/resume``."""
    principal = principal_of(request)
    require_role(principal, {"audit"}, "run a kill switch drill")
    s = services_of(request)
    note = str((body or {}).get("note", ""))[:500]

    def effect(key: str) -> tuple[int, dict[str, Any]]:
        rec = _port_call(s.containment.drill, switch_id, note, principal.role.value, principal.user)
        s.audit.record(principal.role.value, "containment.drill", str(rec.get("incident_id")),
                       {"switch_id": switch_id, "note": note, "user": principal.user})
        return 201, envelope(request, principal, {"incident": dict(rec)})

    return idempotent(request, principal, body or {}, effect)


@router.post("/incidents/{incident_id}/prerequisites", status_code=201)
def prerequisite(request: Request, incident_id: str, body: dict[str, Any] = Body(...)):
    principal = principal_of(request)
    require_role(principal, {"audit"}, "record incident prerequisites")
    s = services_of(request)
    if str(body.get("kind", "")).startswith("correction"):
        raise ApiError(400, "correction_kind_not_allowed", "corrections are recorded only through POST /api/corrections")

    def effect(key: str) -> tuple[int, dict[str, Any]]:
        rec = _port_call(s.containment.record_prerequisite, incident_id, str(body.get("kind", "")),
                         str(body.get("reference", "")), str(body.get("note", "")), principal.role.value)
        s.audit.record(principal.role.value, "incident.prerequisite", incident_id, body)
        return 201, envelope(request, principal, {"prerequisite": dict(rec)})

    return idempotent(request, principal, body, effect)


@router.post("/incidents/{incident_id}/resume")
def resume(request: Request, incident_id: str, body: dict[str, Any] | None = Body(None)):
    body = body or {}
    principal = principal_of(request)
    require_role(principal, {"audit"}, "resume after an incident")
    s = services_of(request)

    def effect(key: str) -> tuple[int, dict[str, Any]]:
        rec = _port_call(s.containment.resume, incident_id, list(body.get("prerequisite_ids", [])), principal.role.value)
        s.audit.record(principal.role.value, "incident.resume", incident_id, body)
        return 200, envelope(request, principal, {"incident": dict(rec)})

    return idempotent(request, principal, body, effect)


@router.post("/corrections/authorisations", status_code=201)
def correction_authorisation(request: Request, body: dict[str, Any] = Body(...)):
    principal = principal_of(request)
    require_role(principal, {"audit"}, "authorise corrections")
    s = services_of(request)

    def effect(key: str) -> tuple[int, dict[str, Any]]:
        rec = _port_call(s.containment.authorise_correction, str(body.get("incident_id", "")), body, principal.role.value)
        s.audit.record(principal.role.value, "correction.authorise", str(rec.get("authorisation_id", "")), body)
        return 201, envelope(request, principal, {"authorisation": dict(rec)})

    return idempotent(request, principal, body, effect)


@router.get("/corrections")
def corrections(request: Request, incident_id: str = Query(...)) -> dict[str, Any]:
    principal = principal_of(request)
    items = [dict(c) for c in services_of(request).containment.corrections(incident_id)]
    return envelope(request, principal, {"incident_id": incident_id, "corrections": items})


@router.get("/corrections/{correction_id}")
def correction(request: Request, correction_id: str) -> dict[str, Any]:
    principal = principal_of(request)
    c = services_of(request).containment.correction(correction_id)
    if c is None:
        raise ApiError(404, "not_found", "correction not found")
    return envelope(request, principal, {"correction": dict(c)})


@router.post("/corrections")
def apply_correction(request: Request, body: dict[str, Any] = Body(...)):
    principal = principal_of(request)
    require_role(principal, {"audit"}, "apply corrections")
    s = services_of(request)
    required = ("incident_id", "authorisation_id", "target_type", "target_id", "item_id", "expected_body_hash")
    missing = [k for k in required if not body.get(k)]
    if missing:
        raise ApiError(400, "invalid_correction", "missing fields: " + ", ".join(missing))
    if body["target_type"] not in {"report_notice", "pr_description", "claim_withdrawal", "external_remediation"}:
        raise ApiError(400, "invalid_correction", "unknown target_type")

    def effect(key: str) -> tuple[int, dict[str, Any]]:
        rec = dict(_port_call(s.containment.apply_correction, body, principal.role.value, key))
        s.audit.record(principal.role.value, "correction.apply", str(rec.get("correction_id", "")), body)
        status = 202 if rec.get("state") == "held" else 200
        return status, envelope(request, principal, {"correction": rec, "status": rec.get("state")})

    return idempotent(request, principal, body, effect)


# ------------------------------------------------------------------ health

@router.get("/health")
def health(request: Request) -> dict[str, Any]:
    s = services_of(request)
    components = {}
    for name in ("snapshots", "tasks", "containment"):
        port = getattr(s, name)
        components[name] = "not_connected" if type(port).__name__ == "Unwired" else "connected"
    components["orchestrator"] = "connected" if s.orchestrator is not None else "not_connected"
    notifier_health = s.notifier.health() if hasattr(s.notifier, "health") else {}
    body = {"status": "ok", "environment": s.environment, "server": f"{s.host}:{s.port}",
            "mode": s.mode.get().value, "identity": getattr(s.identity, "mode", "header"),
            "configuration": dict(getattr(s, "config_presence", {}) or {}), "components": components,
            "notifier": notifier_health, "staged": list(s.staged), "unevaluated": list(s.unevaluated)}
    from datetime import datetime, timezone

    body["data_mode"] = "REAL"
    body["server_time"] = datetime.now(timezone.utc).isoformat()
    return body


__all__ = ["router", "NotWired"]
