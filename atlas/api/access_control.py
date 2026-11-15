"""Read access to single items: open, locked, or open through a grant.

Every item is resolved first, then checked against the caller's project, role and scopes. A locked
item answers 403 with enough to show a lock and a "request access" action, and nothing of its
content. A cross-project or missing item answers 404, exactly alike.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from fastapi import Request

from atlas.access import GRANTED, PENDING, Policy, policy_for, requester_key
from atlas.api.ports import NotWired, Refused
from atlas.api.support import ApiError, check_project, services_of
from atlas.contracts import Principal
from atlas.projection import granted_principal
from atlas.reports import REPORT_TYPES

OPEN, LOCKED = "open", "locked"


class Locked(ApiError):
    """403 for an item the caller may not read; carries what the lock needs, never content."""

    def __init__(self, item: "Item", request_state: Mapping[str, Any] | None) -> None:
        super().__init__(403, "locked", f"this {item.policy.label.lower()} is not open to your role")
        self.item = item
        self.request_state = request_state

    def body(self) -> dict[str, Any]:
        out = super().body()
        out.update(object_type=self.item.object_type, object_id=self.item.object_id, label=self.item.policy.label,
                   access=LOCKED, requestable=self.item.policy.grantable, owner_role=self.item.policy.owner,
                   needs_decision=self.item.policy.sensitive, request=self.request_state)
        return out


@dataclass
class Item:
    object_type: str
    object_id: str
    obj: dict[str, Any]
    scopes: list[str] | None
    policy: Policy
    project_id: str | None


def who(principal: Principal) -> str:
    return requester_key(principal.user, principal.role.value if principal.role else "public")


def _scopes(obj: Mapping[str, Any], default: list[str]) -> list[str] | None:
    if "required_scopes" in obj:
        value = obj["required_scopes"]
    elif "access_scopes" in obj:
        value = obj["access_scopes"]
    else:
        return list(default)
    return None if value is None else list(value)


def _load(request: Request, object_type: str, object_id: str) -> tuple[dict[str, Any] | None, str | None, list[str]]:
    """The stored item, its subtype and its default scopes; None when it does not exist."""
    from atlas.api.routes_runs import run_view

    s = services_of(request)
    try:
        if object_type == "task":
            t = s.tasks.get(object_id)
            return (dict(t) if t else None), None, ["internal"]
        if object_type == "approval":
            a = s.tasks.approval(object_id)
            return (dict(a) if a else None), None, ["internal"]
        if object_type == "run":
            return run_view(request, object_id), None, []
        if object_type in ("report", "finding"):
            if ":" not in object_id:
                return None, None, []
            run_id, tail = object_id.rsplit(":", 1)
            view = run_view(request, run_id)
            auth = s.orchestrator.runs.authoritative(run_id) if (view and s.orchestrator is not None) else None
            if auth is None:
                return None, None, []
            gen, artefact = auth
            if object_type == "report":
                if tail not in REPORT_TYPES or tail not in {r["report_type"] for r in view["reports"]}:
                    return None, None, []
                return ({"report_id": object_id, "run_id": run_id, "report_type": tail,
                         "project_id": view.get("project_id"),
                         "required_scopes": artefact.get("required_scopes", [])}, tail, [])
            findings = list(artefact.get("findings") or [])
            if not tail.isdigit() or int(tail) >= len(findings):
                return None, None, []
            f = dict(findings[int(tail)])
            f.update(finding_id=object_id, run_id=run_id, project_id=view.get("project_id"),
                     authoritative_generation=gen)
            return f, None, ["internal"]
        if object_type == "graph":
            snap = s.snapshots.get(object_id)
            return (dict(snap) if snap else None), None, []
        if object_type == "kill_switch":
            # A switch exists whether or not it is tripped; word ids and row numbers both work.
            state = s.containment.state()
            wanted = str(object_id)
            for sw in state.get("switches", []):
                if not isinstance(sw, Mapping):
                    continue
                if wanted in (str(sw.get("switch_id")), str(sw.get("switch"))):
                    return dict(sw, incidents=[sw["incident"]] if sw.get("incident") else []), None, ["internal"]
            if wanted.isdigit() and 1 <= int(wanted) <= len(state.get("switches", [])):
                sw = state["switches"][int(wanted) - 1]
                return dict(sw, incidents=[sw["incident"]] if sw.get("incident") else []), None, ["internal"]
            try:
                sw = s.containment.switch(object_id) if hasattr(s.containment, "switch") else None
            except Refused:  # an id that names no switch
                sw = None
            return (dict(sw) if sw else None), None, ["internal"]
        if object_type == "incident":
            state = s.containment.state()
            found = [i for i in state.get("incidents", []) if i.get("incident_id") == object_id]
            for sw in state.get("switches", []):
                inc = sw.get("incident") if isinstance(sw, Mapping) else None
                if isinstance(inc, Mapping) and inc.get("incident_id") == object_id:
                    found.append(inc)
            if not found and hasattr(s.containment, "incident"):  # a resumed incident is still an item
                closed = s.containment.incident(object_id)
                if closed:
                    found.append(closed)
            return (dict(found[0]) if found else None), None, ["internal"]
        if object_type == "scorecard_row":
            for row in s.tasks.scorecard().get("rows", []):
                if object_id in (str(row.get("row_id")), str(row.get("task_id"))):
                    return dict(row, required_scopes=["internal"]), None, ["internal"]
            return None, None, ["internal"]
        if object_type == "evidence":
            # ``<run id>::<record id>``: evidence is always read inside the run that used it.
            if "::" not in object_id:
                return None, None, []
            run_id, record_id = object_id.split("::", 1)
            view = run_view(request, run_id)
            rec = s.snapshots.evidence(view["snapshot_id"], record_id, None, ()) if view else None
            if rec is None:
                return None, None, []
            rec = dict(rec)
            rec.setdefault("required_scopes", list(rec.get("access_scopes", ["internal"])))
            rec.update(run_id=run_id, project_id=view.get("project_id"))
            return rec, None, ["internal"]
        if object_type == "dead_letter":
            return ({"dead_letter": object_id} if object_id == "list" else None), None, ["internal"]
    except NotWired:
        return None, None, []
    return None, None, []


def resolve(request: Request, principal: Principal, object_type: str, object_id: str) -> Item:
    """Find the item and check the project. Missing, unknown kind and other-project all answer 404."""
    obj, subtype, default = _load(request, object_type, object_id)
    obj = dict(check_project(request, principal, obj, "item"))
    scopes = _scopes(obj, default)
    policy = policy_for(object_type, subtype, scopes)
    if policy is None:
        raise ApiError(404, "not_found", "item not found")
    return Item(object_type, object_id, obj, scopes, policy, obj.get("project_id") or None)


def _project_key(principal: Principal, item: Item) -> str:
    """The project a request or grant for this item is filed under (the same rule as when it is made)."""
    from atlas.projects import ANY_PROJECT

    if item.project_id:
        return item.project_id
    return "" if principal.project_id in (None, ANY_PROJECT) else str(principal.project_id)


def readable_without_grant(principal: Principal, item: Item) -> bool:
    if principal.role is None or item.scopes is None:
        return False
    return principal.role.value in item.policy.readers and set(item.scopes) <= set(principal.scopes)


def grant_of(request: Request, principal: Principal, item: Item) -> dict[str, Any] | None:
    if principal.role is None or not item.policy.grantable:
        return None
    return services_of(request).access.active_grant(who(principal), _project_key(principal, item),
                                                    item.object_type, item.object_id)


def request_state(request: Request, principal: Principal, item: Item) -> dict[str, Any] | None:
    """The caller's own latest request for this item, if any (id and status only)."""
    if principal.role is None:
        return None
    latest = services_of(request).access.latest_for(who(principal), _project_key(principal, item),
                                                    item.object_type, item.object_id)
    if latest is None:
        return None
    return {"request_id": latest["request_id"], "status": latest["status"]}


def reader_for(request: Request, principal: Principal, object_type: str, object_id: str) -> tuple[Item, Principal, dict[str, Any] | None]:
    """Authorise a single-item read. Returns the item, the principal to project with and the grant.

    Raises ``Locked`` when the item is neither open to the role nor granted to this caller.
    """
    item = resolve(request, principal, object_type, object_id)
    if readable_without_grant(principal, item):
        return item, principal, None
    grant = grant_of(request, principal, item)
    if grant is None:
        raise Locked(item, request_state(request, principal, item))
    return item, granted_principal(principal, item.scopes or ()), grant


def access_of(request: Request, principal: Principal, object_type: str, object_id: str) -> dict[str, Any] | None:
    """One related-item entry for the caller: type, neutral label and open or locked. None if absent."""
    try:
        item = resolve(request, principal, object_type, object_id)
    except ApiError:
        return None
    entry: dict[str, Any] = {"object_type": object_type, "object_id": object_id, "label": item.policy.label,
                             "owner_role": item.policy.owner, "needs_decision": item.policy.sensitive}
    state = request_state(request, principal, item)
    if readable_without_grant(principal, item):
        entry.update(access=OPEN, via_grant=False, requestable=False, request=None)
        return entry
    grant = grant_of(request, principal, item)
    if grant is not None:
        entry.update(access=OPEN, via_grant=True, requestable=False, request=state, expires_at=grant["expires_at"])
        return entry
    entry.update(access=LOCKED, via_grant=False, requestable=item.policy.grantable, request=state)
    return entry


__all__ = ["GRANTED", "Item", "LOCKED", "Locked", "OPEN", "PENDING", "access_of", "grant_of", "reader_for",
           "readable_without_grant", "request_state", "resolve", "who"]
