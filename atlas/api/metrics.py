"""Live project metrics: computed at request time from the real stores, through the same projected reads
the screens use, so a role never sees a number it could not see on the page. A ``project.metrics_changed``
event tells clients to refetch whenever something behind a number changes.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from fastapi import APIRouter, Request

from atlas.api import routes_ops, routes_runs, routes_tasks
from atlas.api.routes_projects import _feed, _member
from atlas.api.streams import PROJECT_STREAM
from atlas.api.support import ApiError, envelope, services_of

router = APIRouter()

# Events that change at least one number, and the metric keys they touch.
TRIGGERS: dict[str, tuple[str, ...]] = {
    "run.status_changed": ("runs_by_status", "needs_a_person", "open_forecast_items"),
    "run.generation_changed": ("open_forecast_items",),
    "task.state_changed": ("approvals_pending", "needs_a_person"),
    "approval.voided": ("approvals_pending",),
    "notification.created": ("unread_notifications",),
    "containment.tripped": ("kill_switches_tripped",),
    "containment.resumed": ("kill_switches_tripped",),
    "project.activity": ("dashboard", "last_poll"),
    "project.dashboard_changed": ("dashboard", "last_poll"),
}
SPEND_STEP_USD = 0.05


class MetricsClock:
    """Per-project metrics version and the hook that publishes ``project.metrics_changed``."""

    def __init__(self, hub: Any, default_project: str | None) -> None:
        self.hub = hub
        self.default_project = default_project
        self._versions: dict[str, int] = {}
        self._lock = threading.Lock()
        self._busy = threading.local()

    def version(self, project_id: str) -> int:
        return self._versions.get(project_id, 0)

    def changed(self, project_id: str | None, keys: Sequence[str]) -> None:
        pid = project_id or self.default_project
        if not pid or getattr(self._busy, "on", False):
            return
        with self._lock:
            self._versions[pid] = self._versions.get(pid, 0) + 1
            v = self._versions[pid]
        wire = {"event": "project.metrics_changed", "event_id": f"metrics-{pid}-{v}",
                "emitted_at": datetime.now(timezone.utc).isoformat(), "run_id": None,
                "authoritative_generation": None, "report_version": None, "correction_version": None,
                "data": {"project_id": pid, "metrics_version": v, "changed": sorted(set(keys))}}
        self._busy.on = True
        try:
            self.hub.publish(PROJECT_STREAM, wire, ["public"], project_id=pid)
        finally:
            self._busy.on = False


def install(services: Any) -> MetricsClock:
    """Hooks the event hub and the spend meter so every change behind a number emits one event."""
    existing = getattr(services, "metrics", None)
    if isinstance(existing, MetricsClock):
        return existing
    clock = MetricsClock(services.hub, getattr(services, "default_project_id", None))
    services.metrics = clock
    original = services.hub.publish

    def publish(stream: str, wire: Mapping[str, Any], required_scopes: Sequence[str],
                roles: Sequence[str] | None = None, project_id: str | None = None) -> None:
        original(stream, wire, required_scopes, roles=roles, project_id=project_id)
        keys = TRIGGERS.get(str(wire.get("event")))
        if keys:
            clock.changed(project_id, keys)

    services.hub.publish = publish
    spend = getattr(services, "spend", None)
    if spend is not None and hasattr(spend, "add"):
        add = spend.add
        state = {"last": float(getattr(spend, "spent", 0.0))}

        def add_and_tell(usd: float) -> None:
            add(usd)
            now = float(getattr(spend, "spent", 0.0))
            if now - state["last"] >= SPEND_STEP_USD:
                state["last"] = now
                clock.changed(None, ("spend",))

        spend.add = add_and_tell
    return clock


def _safe(fn: Any, *args: Any, **kwargs: Any) -> Mapping[str, Any] | None:
    try:
        return fn(*args, **kwargs)
    except ApiError:
        return None
    except Exception as exc:  # a role that may not read a list simply gets no number for it
        if getattr(exc, "status_code", None) in (401, 403, 404) or exc.__class__.__name__ == "IdentityError":
            return None
        raise


def compute(request: Request, project_id: str) -> dict[str, Any]:
    s = services_of(request)
    runs = _safe(routes_runs.runs, request)
    run_list = list((runs or {}).get("runs", []))
    by_status: dict[str, int] = {}
    for r in run_list:
        by_status[str(r.get("run_status"))] = by_status.get(str(r.get("run_status")), 0) + 1
    forecast_items = None
    for r in reversed(run_list):
        rep = next((x for x in r.get("reports", []) if x.get("report_type") == "forecast"), None)
        if rep:
            body = _safe(routes_runs.report, request, rep["report_id"], generation=None)
            items = (((body or {}).get("report") or {}).get("forecast") or {}).get("items")
            forecast_items = len(items) if isinstance(items, list) else None
            break
    queue = _safe(routes_runs.queue, request)
    approvals = _safe(routes_tasks.approvals, request)
    notes = _safe(routes_ops.notifications, request, severity=None, kind=None, state=None, subject_id=None)
    contain = _safe(routes_ops.containment, request)
    board = _feed(request).dashboard(project_id)
    spend = getattr(s, "spend", None)
    return {
        "open_forecast_items": forecast_items,
        "runs_total": len(run_list) if runs is not None else None,
        "runs_by_status": by_status if runs is not None else None,
        "needs_a_person": len((queue or {}).get("entries", [])) if queue is not None else None,
        "approvals_pending": (len([a for a in (approvals or {}).get("approvals", []) if a.get("state") in ("pending", "requested")])
                              if approvals is not None else None),
        "kill_switches_tripped": (len([x for x in (contain or {}).get("switches", []) if x.get("state") != "ok"])
                                  if contain is not None else None),
        "kill_switches_total": len((contain or {}).get("switches", [])) if contain is not None else None,
        "unread_notifications": (len([n for n in (notes or {}).get("notifications", []) if not n.get("acknowledged")])
                                 if notes is not None else None),
        "spend_used_usd": round(float(spend.spent), 4) if spend is not None and hasattr(spend, "spent") else None,
        "spend_ceiling_usd": float(spend.ceiling) if spend is not None and hasattr(spend, "ceiling") else None,
        "spend_scope": "installation",
        "last_poll": board.get("last_poll"),
        "dashboard_version": board.get("dashboard_version"),
        "dashboard_counts": {p["panel_id"]: len(p.get("items", [])) for p in board.get("panels", [])},
    }


@router.get("/projects/{project_id}/metrics")
def metrics(request: Request, project_id: str) -> dict[str, Any]:
    principal = _member(request, project_id)
    clock = install(services_of(request))
    body = compute(request, project_id)
    body.update({"project_id": project_id, "metrics_version": clock.version(project_id),
                 "computed_at": datetime.now(timezone.utc).isoformat()})
    return envelope(request, principal, body)
