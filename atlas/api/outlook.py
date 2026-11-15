"""The release outlook: two product questions answered from the real stores, for every page.

* Q1, at release close: what technical debt and issues the next release inherits, and who should take
  each item (developers, the coding agent, or a person decides first). Read from the latest verified
  release close of the project.
* Q2, mid-release: what stops shipping now, which services it touches, and the delivery status, with
  the rule that set it.

Nothing here is invented: a missing input is reported as missing, the forecast date range stays
"illustrative" and no confidence is added. ``project.outlook_changed`` tells clients to refetch when
something behind the outlook changes.

Delivery status rules (checked in this order; see docs/guide/release-outlook.md):

1. BLOCKED when the security gate is blocked (the live dependency scan has at least one blocking
   finding), or an investigation that escalated a decision is still waiting for a person.
2. AT_RISK when failing CI jobs reach ``AT_RISK_FAILING_CI``, at least one open critical issue
   exists, or an investigation held for a person is still waiting.
3. NOT_MEASURED when no rule above fired and at least one input is missing.
4. ON_TRACK when every input is measured and no rule fired.
"""

from __future__ import annotations

import re
import threading
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from fastapi import APIRouter, Request

from atlas.access import KINDS
from atlas.api import catalog as cat
from atlas.api.navigation import OUTLOOK_SECTIONS
from atlas.api.routes_projects import _feed, _member
from atlas.api.snapshot_labels import describe, kind_of
from atlas.api.streams import PROJECT_STREAM
from atlas.api.support import ApiError, envelope, services_of

router = APIRouter()

AT_RISK_FAILING_CI = 3
AT_RISK_CRITICAL_ISSUES = 1
SECURITY_COMPONENT = "project-dependencies"
NOT_MAPPED = "not mapped"
OWNERS = {"developer": "developers", "coding_agent": "coding_agents", "decision_maker": "decide_first"}

# Events that can change an input of the outlook.
TRIGGERS: dict[str, tuple[str, ...]] = {
    "run.status_changed": ("q1", "q2"),
    "run.generation_changed": ("q1", "q2"),
    "report.superseded": ("q1",),
    "report.corrected": ("q1",),
    "report.withdrawn": ("q1",),
    "project.dashboard_changed": ("q2",),
}

STATUS_WORDS = {"ON_TRACK": "On track", "AT_RISK": "At risk", "BLOCKED": "Blocked", "NOT_MEASURED": "Not measured"}


class OutlookClock:
    """Per-project outlook version and the hook that publishes ``project.outlook_changed``."""

    def __init__(self, hub: Any, default_project: str | None) -> None:
        self.hub = hub
        self.default_project = default_project
        self._versions: dict[str, int] = {}
        self._lock = threading.Lock()
        self._busy = threading.local()

    def version(self, project_id: str) -> int:
        return self._versions.get(project_id, 0)

    def changed(self, project_id: str | None, parts: Sequence[str]) -> None:
        pid = project_id or self.default_project
        if not pid or getattr(self._busy, "on", False):
            return
        with self._lock:
            self._versions[pid] = self._versions.get(pid, 0) + 1
            v = self._versions[pid]
        wire = {"event": "project.outlook_changed", "event_id": f"outlook-{pid}-{v}",
                "emitted_at": datetime.now(timezone.utc).isoformat(), "run_id": None,
                "authoritative_generation": None, "report_version": None, "correction_version": None,
                "data": {"project_id": pid, "outlook_version": v, "changed": sorted(set(parts))}}
        self._busy.on = True
        try:
            self.hub.publish(PROJECT_STREAM, wire, ["public"], project_id=pid)
        finally:
            self._busy.on = False


def install(services: Any) -> OutlookClock:
    """Hooks the event hub (and recorded decisions) so a change behind the outlook emits one event."""
    existing = getattr(services, "outlook", None)
    if isinstance(existing, OutlookClock):
        return existing
    clock = OutlookClock(services.hub, getattr(services, "default_project_id", None))
    services.outlook = clock
    original = services.hub.publish

    def publish(stream: str, wire: Mapping[str, Any], required_scopes: Sequence[str],
                roles: Sequence[str] | None = None, project_id: str | None = None) -> None:
        original(stream, wire, required_scopes, roles=roles, project_id=project_id)
        parts = TRIGGERS.get(str(wire.get("event")))
        if parts:
            clock.changed(project_id, parts)

    services.hub.publish = publish
    feedback = getattr(services, "feedback", None)
    if feedback is not None and hasattr(feedback, "record_feedback"):
        record = feedback.record_feedback

        def record_and_tell(run_id: str, *args: Any, **kwargs: Any) -> Any:
            out = record(run_id, *args, **kwargs)
            pid = None
            adm = services.admissions.get(run_id) if hasattr(services.admissions, "get") else None
            if adm is not None:
                pid = getattr(adm, "project_id", None)
            clock.changed(pid, ("q2",))
            return out

        feedback.record_feedback = record_and_tell
    return clock


# ------------------------------------------------------------------ inputs


def _runs(request: Request, project_id: str) -> list[dict[str, Any]]:
    from atlas.api.routes_runs import run_view
    from atlas.scheduler import ADMITTED, DISPATCHED, DISPATCHING, HELD

    s = services_of(request)
    out = []
    for a in s.admissions.by_status(ADMITTED, HELD, DISPATCHING, DISPATCHED):
        if (a.project_id or s.default_project_id) not in (project_id, None, ""):
            continue
        view = run_view(request, a.run_id)
        if view is not None and (view.get("project_id") or project_id) == project_id:
            out.append(view)
    return out


def _artefact(s: Any, run_id: str) -> dict[str, Any] | None:
    if s.orchestrator is None:
        return None
    auth = s.orchestrator.runs.authoritative(run_id)
    return dict(auth[1]) if auth else None


def _reads(role: str | None, kind: str) -> bool:
    rule = KINDS.get(kind)
    return bool(role and rule and role in rule.readers)


def _release_snapshots(s: Any, project_id: str) -> list[dict[str, Any]]:
    try:
        snaps = [dict(x) for x in s.snapshots.list(project_id=project_id)]
    except Exception:  # the data layer may be missing; the outlook then says so
        return []
    cfg = s.projects.get(project_id) if hasattr(s.projects, "get") else None
    name = getattr(cfg, "name", None) or project_id.replace("-", " ").title()
    out = [describe(s.snapshots, x, name) for x in snaps if kind_of(x) == "release"]
    out = [x for x in out if not x.get("is_test_copy")]
    return sorted(out, key=lambda x: str(x.get("as_of") or ""), reverse=True)


def _snapshot_as_of(s: Any, snapshot_id: str | None) -> str | None:
    if not snapshot_id:
        return None
    try:
        snap = s.snapshots.get(snapshot_id) or {}
    except Exception:
        return None
    value = snap.get("cutoff") or snap.get("frozen_at")
    return str(value) if value else None


def _q1(request: Request, principal: Any, project_id: str, runs: list[dict[str, Any]]) -> dict[str, Any]:
    s = services_of(request)
    role = principal.role.value if principal.role else None
    closes = [r for r in runs if r.get("kind") == "release_close"]
    closes.sort(key=lambda r: str(r.get("started_at") or ""), reverse=True)
    verified = [r for r in closes if r.get("run_status") == "complete" and _artefact(s, r["run_id"])]
    # The next release inherits from the LATEST release, not from the newest run: a re-run of an
    # older release (a backtest) must not replace the current one. Newest run wins within a release.
    verified.sort(key=lambda r: (str(_snapshot_as_of(s, r.get("snapshot_id")) or ""),
                                 str(r.get("started_at") or "")), reverse=True)
    trigger = cat.BY_ID["release_close"]
    try:
        ok, reason = cat.availability(trigger, principal, mode=s.mode.get(), permits=s.containment.permits,
                                      model_configured=s.model_configured)
    except Exception as exc:  # containment not connected: the button stays off and says why
        ok, reason = False, str(exc)
    releases = _release_snapshots(s, project_id)
    run_action = {"allowed": ok, "reason": None if ok else reason,
                  "link": (f"/run-now?snapshot={releases[0]['snapshot_id']}" if releases else "/run-now"),
                  "dataset": releases[0].get("title") if releases else None}
    in_progress = next((r for r in closes if r.get("run_status") not in ("complete", "failed", "unverified", "partial")),
                       None)
    base: dict[str, Any] = {"run_action": run_action,
                            "in_progress": {"run_id": in_progress["run_id"], "link": f"/runs/{in_progress['run_id']}"}
                            if in_progress else None}
    if not verified:
        return {**base, "state": "none", "message": "No release close yet", "items": [], "total": 0,
                "split": {k: 0 for k in (*OWNERS.values(), "not_allocated")}, "dataset": None}
    run = verified[0]
    art = _artefact(s, run["run_id"]) or {}
    f = art.get("forecast") if isinstance(art.get("forecast"), Mapping) else {}
    raw = [dict(i) for i in (f.get("items") or art.get("items") or [])]
    for i in raw:
        i.setdefault("required_scopes", ["public"])
        i["project_id"] = project_id
    visible = s.projection.project({"required_scopes": [], "items": raw}, principal).get("items") or []
    report_id = f"{run['run_id']}:forecast"
    alloc_id = f"{run['run_id']}:allocation"
    items = []
    split = {k: 0 for k in (*OWNERS.values(), "not_allocated")}
    for i in visible:
        owner = i.get("allocation")
        bucket = OWNERS.get(str(owner), "not_allocated")
        split[bucket] += 1
        evidence = list(i.get("evidence") or [])
        items.append({
            "id": i.get("id"), "issue_id": i.get("issue_id"),
            "title": i.get("title") or (f"Issue {i.get('issue_id')}" if i.get("issue_id") else "Untitled item"),
            "component": i.get("component"), "owner_team": i.get("owner_team"), "score": i.get("score"),
            "owner": bucket, "reason": i.get("allocation_reason"),
            "evidence_link": f"/evidence/{evidence[0]}?run={run['run_id']}" if evidence else None,
        })
    snapshot_id = run.get("snapshot_id")
    verifier = art.get("verifier") or {}
    return {
        **base, "state": "ready", "message": None,
        "run_id": run["run_id"], "run_link": f"/runs/{run['run_id']}",
        "report_link": f"/reports/{report_id}", "report_access": "open" if _reads(role, "report:forecast") else "locked",
        "allocation_link": f"/reports/{alloc_id}",
        "allocation_access": "open" if _reads(role, "report:allocation") else "locked",
        "dataset": {"title": run.get("dataset_title"), "snapshot_id": snapshot_id,
                    "as_of": _snapshot_as_of(s, snapshot_id)},
        "closed_at": run.get("started_at"),
        "basis": "predicted",
        "date_range": f.get("date_range") or "illustrative",
        "total": len(items), "withheld": len(raw) - len(visible), "split": split, "items": items,
        "claims_checked": verifier.get("claims_checked"), "claims_rejected": verifier.get("claims_rejected"),
        "data_mode": art.get("data_mode", "REAL"),
    }


_SIG = re.compile(r"(?:^|[-_/])sig-([a-z0-9]+)")


def job_component(job: str) -> str | None:
    """The component a CI job tests, from the ``sig-<name>`` part of its name; None when it names none."""
    m = _SIG.search(job.lower())
    return m.group(1) if m else None


def _label_component(labels: Sequence[str]) -> str | None:
    return next((str(lab).split("/", 1)[1] for lab in sorted(labels) if str(lab).startswith("sig/")), None)


def _critical_issues(s: Any, project_id: str, panel: Mapping[str, Any] | None) -> tuple[list[dict[str, Any]] | None, str]:
    """Open issues that carry the project's critical label. None when the input is missing."""
    cfg = s.projects.get(project_id) if hasattr(s.projects, "get") else None
    label = str((getattr(cfg, "labels", {}) or {}).get("critical") or "")
    if not label:
        return None, "the project names no critical label"
    rows: list[Mapping[str, Any]] | None = None
    poller = (getattr(s.snapshots, "pollers", None) or {}).get(project_id)
    if poller is not None:
        try:
            db = poller.registry.live(project_id)
            rows = []
            for rec in db.search({"source": "issues", "limit": 100_000}):
                view = db.content(rec.record_id) or {}
                if isinstance(view, Mapping) and str(view.get("state", "open")).lower() == "open":
                    rows.append(dict(view, record_id=rec.record_id, content_version_id=rec.content_version_id))
        except Exception:
            rows = None
    if rows is None:
        if not panel or panel.get("status") != "live":
            return None, "the live issue list is not connected"
        rows = [dict(r) for r in panel.get("items", []) if "number" in r]
    latest: dict[Any, Mapping[str, Any]] = {}
    for r in rows:
        latest[r.get("number")] = r
    out = []
    for r in latest.values():
        labels = [str(x) for x in r.get("labels", [])]
        if label in labels:
            out.append({"number": r.get("number"), "title": r.get("title"), "component": _label_component(labels),
                        "record_id": r.get("record_id"), "content_version_id": r.get("content_version_id")})
    out.sort(key=lambda r: int(r["number"]) if str(r.get("number") or "").isdigit() else 0, reverse=True)
    return out, label


def _panel(board: Mapping[str, Any], panel_id: str) -> Mapping[str, Any] | None:
    return next((p for p in board.get("panels", []) if p.get("panel_id") == panel_id), None)


def _q2(request: Request, principal: Any, project_id: str, runs: list[dict[str, Any]]) -> dict[str, Any]:
    s = services_of(request)
    role = principal.role.value if principal.role else None
    board = _feed(request).dashboard(project_id)
    missing: list[dict[str, str]] = []
    blockers: list[dict[str, Any]] = []
    inputs: dict[str, Any] = {}

    # Failing CI jobs (live dashboard).
    ci = _panel(board, "failing_ci")
    if ci and ci.get("status") == "live":
        jobs = [j for j in ci.get("items", []) if j.get("job")]
        count = int(ci.get("total", len(jobs)))
        inputs["failing_ci"] = {"count": count, "link": "/dashboard#failing_ci",
                                "note": next((j.get("note") for j in ci.get("items", []) if j.get("note")), None)}
        for j in jobs:
            blockers.append({"kind": "failing_ci", "title": str(j.get("job")), "component": job_component(str(j["job"])),
                             "effect": "risk", "link": "/dashboard#failing_ci", "at": j.get("finished_at")})
    else:
        inputs["failing_ci"] = None
        missing.append({"input": "failing_ci", "reason": (ci or {}).get("reason") or "the live CI panel is not connected"})

    # Security gate (live dependency scan). Finding details stay with the security role; counts are open.
    sec = _panel(board, "security")
    rows = [r for r in (sec or {}).get("items", []) if "finding_id" in r]
    note = next((r.get("note") for r in (sec or {}).get("items", []) if r.get("note")), "") or ""
    scanned = bool(sec and sec.get("status") == "live" and (rows or "scanned" in note))
    if scanned:
        blocking = sum(1 for r in rows if r.get("blocking"))
        if not rows:
            m = re.search(r"(\d+) blocking", note)
            blocking = int(m.group(1)) if m else 0
        total = int((sec or {}).get("total", len(rows)))
        state = "blocked" if blocking else "passed"
        inputs["security_gate"] = {"state": state, "blocking_findings": blocking, "findings": total,
                                   "link": "/dashboard#security", "note": note or None}
        if blocking:
            blockers.append({"kind": "security", "title": f"{blocking} blocking dependency finding{'s' if blocking != 1 else ''}",
                             "component": SECURITY_COMPONENT, "effect": "blocks", "link": "/dashboard#security",
                             "count": blocking})
    else:
        inputs["security_gate"] = None
        missing.append({"input": "security_gate", "reason": note or (sec or {}).get("reason")
                        or "no completed dependency scan"})

    # Investigations waiting for a person.
    decided = set()
    if hasattr(s.feedback, "runs_with_decisions"):
        try:
            decided = s.feedback.runs_with_decisions()
        except Exception:
            decided = set()
    waiting = []
    measured_runs = s.orchestrator is not None
    for r in runs:
        if r.get("kind") != "failure" or r.get("run_status") != "complete" or r["run_id"] in decided:
            continue
        art = _artefact(s, r["run_id"]) or {}
        action = (art.get("action") or {}).get("value")
        held = art.get("safety_disposition") == "HOLD_FOR_HUMAN"
        if action != "ESCALATE_DECISION" and not held:
            continue
        comps = list((r.get("trigger") or {}).get("components") or [])
        effect = "blocks" if action == "ESCALATE_DECISION" else "risk"
        waiting.append(r["run_id"])
        blockers.append({"kind": "investigation", "title": r.get("title") or r["run_id"],
                         "component": comps[0] if comps else None, "effect": effect,
                         "reason": "a release decision is waiting" if effect == "blocks" else "held for a person",
                         "link": f"/runs/{r['run_id']}", "at": r.get("started_at")})
    escalated = sum(1 for b in blockers if b["kind"] == "investigation" and b["effect"] == "blocks")
    held_n = sum(1 for b in blockers if b["kind"] == "investigation" and b["effect"] == "risk")
    if measured_runs:
        inputs["investigations"] = {"escalated": escalated, "held": held_n, "link": "/runs"}
    else:
        inputs["investigations"] = None
        missing.append({"input": "investigations", "reason": "the run engine is not connected"})

    # Open critical issues.
    critical, label = _critical_issues(s, project_id, _panel(board, "open_issues"))
    if critical is None:
        inputs["critical_issues"] = None
        missing.append({"input": "critical_issues", "reason": label})
    else:
        inputs["critical_issues"] = {"count": len(critical), "label": label, "link": "/dashboard#open_issues"}
        for c in critical:
            link = (f"/activity-evidence/{c['record_id']}?project={project_id}&version={c.get('content_version_id') or ''}"
                    if c.get("record_id") else "/dashboard#open_issues")
            blockers.append({"kind": "critical_issue", "title": f"#{c['number']} {c.get('title') or ''}".strip(),
                             "component": c.get("component"), "effect": "risk", "link": link})

    # Delivery status.
    reasons: list[dict[str, str]] = []
    gate = inputs.get("security_gate")
    if gate and gate["state"] == "blocked":
        reasons.append({"rule": "security_gate_blocked", "status": "BLOCKED",
                        "text": f"Security gate blocked: {gate['blocking_findings']} blocking finding(s)"})
    if escalated:
        reasons.append({"rule": "decision_waiting", "status": "BLOCKED",
                        "text": f"{escalated} investigation(s) wait for a release decision"})
    ci_in = inputs.get("failing_ci")
    if ci_in and ci_in["count"] >= AT_RISK_FAILING_CI:
        reasons.append({"rule": "failing_ci", "status": "AT_RISK",
                        "text": f"{ci_in['count']} failing CI jobs (at risk from {AT_RISK_FAILING_CI})"})
    crit_in = inputs.get("critical_issues")
    if crit_in and crit_in["count"] >= AT_RISK_CRITICAL_ISSUES:
        reasons.append({"rule": "critical_issues", "status": "AT_RISK",
                        "text": f"{crit_in['count']} open critical issue(s)"})
    if held_n:
        reasons.append({"rule": "held_for_person", "status": "AT_RISK",
                        "text": f"{held_n} investigation(s) held for a person"})
    if any(r["status"] == "BLOCKED" for r in reasons):
        status = "BLOCKED"
    elif reasons:
        status = "AT_RISK"
    elif missing:
        status = "NOT_MEASURED"
        reasons.append({"rule": "inputs_missing", "status": "NOT_MEASURED",
                        "text": "Not measured: " + ", ".join(m["input"].replace("_", " ") for m in missing)})
    else:
        status = "ON_TRACK"
        reasons.append({"rule": "all_clear", "status": "ON_TRACK", "text": "Every input measured; no rule fired"})

    graph_ok = _reads(role, "graph")
    services_map: dict[str, dict[str, Any]] = {}
    for b in blockers:
        name = b.get("component") or NOT_MAPPED
        entry = services_map.setdefault(name, {"component": name, "blockers": 0, "blocks": 0, "kinds": {},
                                               "link": ("/graph" if graph_ok else "/dashboard")
                                               if name != NOT_MAPPED else None})
        weight = int(b.get("count", 1)) if b["kind"] == "security" else 1
        entry["blockers"] += weight
        entry["blocks"] += weight if b["effect"] == "blocks" else 0
        entry["kinds"][b["kind"]] = entry["kinds"].get(b["kind"], 0) + weight
    affected = sorted(services_map.values(), key=lambda e: (-e["blocks"], -e["blockers"], e["component"]))
    mapped = [e for e in affected if e["component"] != NOT_MAPPED]
    return {
        "status": status, "status_word": STATUS_WORDS[status], "reasons": reasons, "missing_inputs": missing,
        "inputs": inputs, "blockers": blockers,
        "blocker_count": len(blockers), "blocking_count": sum(1 for b in blockers if b["effect"] == "blocks"),
        "affected_services": affected, "service_count": len(mapped),
        "thresholds": {"failing_ci_at_risk": AT_RISK_FAILING_CI, "critical_issues_at_risk": AT_RISK_CRITICAL_ISSUES},
        "last_poll": board.get("last_poll"), "graph_link": "/graph" if graph_ok else None,
    }


def compute(request: Request, principal: Any, project_id: str) -> dict[str, Any]:
    role = principal.role.value if principal.role else ""
    sections = OUTLOOK_SECTIONS.get(role, ())
    runs = _runs(request, project_id)
    return {
        "sections": list(sections),
        "q1": _q1(request, principal, project_id, runs) if "q1" in sections else None,
        "q2": _q2(request, principal, project_id, runs) if "q2" in sections else None,
    }


@router.get("/projects/{project_id}/outlook")
def outlook(request: Request, project_id: str) -> dict[str, Any]:
    principal = _member(request, project_id)
    role = principal.role.value if principal.role else ""
    if role not in OUTLOOK_SECTIONS:
        raise ApiError(403, "forbidden", "the release outlook is for the product manager, engineering manager "
                                         "and solution architect")
    clock = install(services_of(request))
    body = compute(request, principal, project_id)
    body.update({"project_id": project_id, "outlook_version": clock.version(project_id),
                 "computed_at": datetime.now(timezone.utc).isoformat()})
    return envelope(request, principal, body)
