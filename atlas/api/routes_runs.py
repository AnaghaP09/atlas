"""Triggers, schedules, snapshots, runs, reports, evidence, graph, queue and feedback."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Body, Query, Request

from atlas.api import catalog as cat
from atlas.api.snapshot_labels import describe
from atlas.api.identity import principal_of, require_role
from atlas.api.ports import NotWired, Refused
from atlas.api.support import ApiError, check_project, envelope, idempotent, services_of
from atlas.contracts import StartedBy, StartedByKind
from atlas.projects import ANY_PROJECT
from atlas.reports import REPORT_TYPES, render
from atlas.scheduler import manual_admission

router = APIRouter()

REPORTS_FOR = {
    "forecast": ("forecast", "allocation", "architecture", "security_gate"),
    "investigation": ("investigation", "security_gate"),
    "question": ("question",),
}


# ------------------------------------------------------------------ helpers

def _project(principal: Any, s: Any = None, body: dict[str, Any] | None = None) -> str:
    """The project a request acts in comes from the caller's identity (the ``project_id`` query or
    path parameter checked against membership), never from the request body. Only a demo caller in
    an installation with no projects configured may name one in the body."""
    if principal.project_id == ANY_PROJECT:
        return str((body or {}).get("project_id") or (s.default_project_id if s is not None else "") or "")
    if not principal.project_id:
        raise ApiError(400, "project_required",
                       "name the project (?project_id=...) or set a default project in the configuration")
    return str(principal.project_id)


def _availability(request: Request, principal: Any, trigger: cat.ManualTrigger) -> tuple[bool, str | None]:
    s = services_of(request)
    return cat.availability(trigger, principal, mode=s.mode.get(), permits=s.containment.permits,
                            model_configured=s.model_configured)


def _require_available(request: Request, principal: Any, trigger_id: str) -> None:
    ok, reason = _availability(request, principal, cat.BY_ID[trigger_id])
    if not ok:
        if reason == "role":
            raise ApiError(403, "forbidden", f"the {principal.role.value} role may not use this trigger")
        raise ApiError(403, "trigger_unavailable", reason or "unavailable")


def _snapshot(request: Request, principal: Any, snapshot_id: str | None) -> dict[str, Any]:
    if not snapshot_id:
        raise ApiError(400, "snapshot_required", "a run must name the snapshot it is bound to (snapshot_id)")
    snap = services_of(request).snapshots.get(snapshot_id)
    return dict(check_project(request, principal, snap, "snapshot"))


def _admit(request: Request, principal: Any, trigger_id: str, trigger: dict[str, Any], snapshot_id: str,
           body: dict[str, Any]):
    s = services_of(request)
    _require_available(request, principal, trigger_id)
    snap = _snapshot(request, principal, snapshot_id)
    project_id = str(snap.get("project_id") or "") if principal.project_id == ANY_PROJECT else _project(principal)

    def effect(key: str) -> tuple[int, dict[str, Any]]:
        sb = StartedBy(kind=StartedByKind.PERSON, role=principal.role)
        adm = manual_admission(s.admissions, key, trigger, snapshot_id, sb, project_id=project_id)
        s.audit.record(principal.role.value, f"trigger.{trigger_id}", adm.run_id, body)
        s.run_in_background(s.dispatcher.dispatch_pending)
        return 202, envelope(request, principal, {"run_id": adm.run_id, "status": "admitted",
                                                  "started_by": {"kind": "person", "role": principal.role.value},
                                                  "snapshot_id": snapshot_id})

    return idempotent(request, principal, body, effect)


RUN_KIND_LABEL = {"release_close": "Release close", "failure": "Investigation", "question": "Question",
                  "nightly": "Nightly sweep", "reverify": "Re-check"}


def _run_label(s: Any, adm: Any, snap: Any) -> dict[str, Any]:
    """Plain words for a run: what kind it is, what it is about and when it started."""
    trigger = adm.trigger or {}
    kind = str(trigger.get("trigger") or "run")
    dataset = None
    if snap:
        projects = getattr(s, "projects", None)
        cfg = projects.get(snap.get("project_id")) if projects is not None and hasattr(projects, "get") and snap.get("project_id") else None
        name = getattr(cfg, "name", None) or (str(snap.get("project_id")).replace("-", " ").title() if snap.get("project_id") else None)
        try:
            dataset = describe(s.snapshots, snap, name).get("title")
        except Exception:  # a label must never break a run view
            dataset = None
    if kind == "release_close":
        subject = dataset or (f"release {trigger.get('release_id')}" if trigger.get("release_id") else "")
    elif kind == "failure":
        subject = f"issue #{trigger.get('event_id')}" if str(trigger.get("event_id") or "").isdigit() else str(trigger.get("event_id") or "")
    elif kind == "question":
        q = " ".join(str(trigger.get("question") or "").split())
        subject = q if len(q) <= 70 else q[:69].rstrip() + "…"
    elif kind == "nightly":
        subject = f"{len(trigger.get('queued') or [])} queued"
    else:
        subject = ""
    label = RUN_KIND_LABEL.get(kind, kind.replace("_", " ").capitalize())
    return {"kind": kind, "kind_label": label, "subject": subject, "title": f"{label} · {subject}" if subject else label,
            "dataset_title": dataset, "started_at": getattr(adm, "admitted_at", None)}


def run_view(request: Request, run_id: str) -> dict[str, Any] | None:
    s = services_of(request)
    adm = s.admissions.get(run_id)
    if adm is None:
        return None
    try:
        snap = s.snapshots.get(adm.snapshot_id)
    except NotWired:
        snap = None
    auth = s.orchestrator.runs.authoritative(run_id) if s.orchestrator is not None else None
    gen, artefact = auth if auth else (None, None)
    view: dict[str, Any] = {
        "run_id": run_id, "admission_status": adm.status, "hold_reason": adm.hold_reason,
        "trigger": adm.trigger, "started_by": adm.started_by, "snapshot_id": adm.snapshot_id,
        "project_id": adm.project_id or (snap or {}).get("project_id"),
        "authoritative_generation": gen,
        "superseded_generations": list(range(1, gen)) if gen else [],
        "run_status": (artefact or {}).get("run_status") or ("failed" if run_id in s.run_errors else "pending"),
        "error": s.run_errors.get(run_id),
        "cost": (artefact or {}).get("cost"),
        "reports": [],
    }
    view.update(_run_label(s, adm, snap))
    if artefact:
        rtype = artefact.get("report_type", "investigation")
        for t in REPORTS_FOR.get(rtype, (rtype,)):
            view["reports"].append({"report_id": f"{run_id}:{t}", "report_type": t,
                                    "report_version": artefact.get("report_version", 1)})
    return view


# ------------------------------------------------------------------ triggers

@router.get("/triggers/catalog")
def trigger_catalog(request: Request) -> dict[str, Any]:
    principal = principal_of(request)
    s = services_of(request)
    manual = []
    for t in cat.MANUAL_TRIGGERS:
        ok, reason = _availability(request, principal, t)
        manual.append({"trigger_id": t.trigger_id, "title": t.title, "method": t.method, "path": t.path,
                       "allowed_roles": sorted(t.roles),
                       "parameters": [{"name": n, "type": ty, "required": req} for n, ty, req in t.parameters],
                       "availability": "available" if ok else "disabled", "reason": reason})
    automatic = [_schedule_view(s, wid) for wid in s.scheduler.watchers]
    return envelope(request, principal, {"manual": manual, "automatic": automatic})


@router.post("/triggers/release-close", status_code=202)
def trigger_release_close(request: Request, body: dict[str, Any] = Body(...)):
    principal = principal_of(request)
    trigger = {"trigger": "release_close", "release_id": body.get("release_id"),
               "snapshot_as_of": body.get("snapshot_as_of")}
    if not trigger["release_id"]:
        raise ApiError(400, "release_required", "a release-close trigger needs release_id")
    return _admit(request, principal, "release_close", trigger, body.get("snapshot_id"), body)


@router.post("/triggers/failure", status_code=202)
def trigger_failure(request: Request, body: dict[str, Any] = Body(...)):
    return _investigate(request, body)


@router.post("/triggers/manual/investigate", status_code=202)
def trigger_investigate(request: Request, body: dict[str, Any] = Body(...)):
    return _investigate(request, body)


def _investigate(request: Request, body: dict[str, Any]):
    principal = principal_of(request)
    if not body.get("event_id"):
        raise ApiError(400, "event_required", "name the failure or issue to investigate (event_id)")
    trigger = {"trigger": "failure", "event_id": body["event_id"], "summary": body.get("summary", ""),
               "components": body.get("components", [])}
    return _admit(request, principal, "investigate", trigger, body.get("snapshot_id"), body)


@router.post("/triggers/nightly", status_code=202)
def trigger_nightly(request: Request, body: dict[str, Any] = Body(...)):
    principal = principal_of(request)
    trigger = {"trigger": "nightly", "queued": list(body.get("queued", []))}
    return _admit(request, principal, "nightly", trigger, body.get("snapshot_id"), body)


@router.post("/triggers/question", status_code=202)
def trigger_question(request: Request, body: dict[str, Any] = Body(...)):
    principal = principal_of(request)
    if not str(body.get("question", "")).strip():
        raise ApiError(400, "question_required", "a question trigger needs a question")
    trigger = {"trigger": "question", "question": body["question"]}
    return _admit(request, principal, "question", trigger, body.get("snapshot_id"), body)


@router.post("/runs/{run_id}/reverify")
def reverify(request: Request, run_id: str, body: dict[str, Any] | None = Body(None)):
    principal = principal_of(request)
    s = services_of(request)
    _require_available(request, principal, "reverify")
    check_project(request, principal, run_view(request, run_id), "run")
    if s.orchestrator is None or s.orchestrator.runs.authoritative(run_id) is None:
        raise ApiError(409, "not_reverifiable", "this run has no stored verified result to check again")

    def effect(key: str) -> tuple[int, dict[str, Any]]:
        new = s.orchestrator.reverify(run_id)
        try:
            tasks = [dict(t) for t in s.tasks.tasks_for_run(run_id)]
        except Exception:
            tasks = []
        s.audit.record(principal.role.value, "trigger.reverify", run_id, body or {})
        return 200, envelope(request, principal, {"run_id": run_id,
                                                  "authoritative_generation": new.get("authoritative_generation"),
                                                  "superseded_generation": (new.get("authoritative_generation") or 1) - 1,
                                                  "tasks": tasks})

    return idempotent(request, principal, body or {}, effect)


# ------------------------------------------------------------------ schedules

def _schedule_view(s: Any, watcher_id: str) -> dict[str, Any]:
    st = s.scheduler.state(watcher_id)
    w = s.scheduler.watchers[watcher_id]
    state = "enabled" if st["enabled"] and not st.get("paused_reason") else ("paused" if st["enabled"] or st.get("paused_reason") else "disabled")
    return {"schedule_id": watcher_id, "kind": w.kind, "state": state, "reason": st.get("paused_reason"),
            "last_run": st.get("last_run"), "last_event_id": st.get("last_event"), "next_run": None,
            "project_id": getattr(w, "project_id", "") or None}


@router.get("/schedules")
def schedules(request: Request) -> dict[str, Any]:
    principal = principal_of(request)
    s = services_of(request)
    return envelope(request, principal, {"schedules": [_schedule_view(s, w) for w in s.scheduler.watchers]})


@router.post("/schedules/{schedule_id}/{action}")
def schedule_action(request: Request, schedule_id: str, action: str, body: dict[str, Any] | None = Body(None)):
    principal = principal_of(request)
    require_role(principal, {"em", "audit"}, "control schedules")
    s = services_of(request)
    if schedule_id not in s.scheduler.watchers:
        raise ApiError(404, "not_found", "schedule not found")
    check_project(request, principal, {"project_id": getattr(s.scheduler.watchers[schedule_id], "project_id", "")},
                  "schedule")
    if action not in {"enable", "pause", "run-once"}:
        raise ApiError(404, "not_found", "unknown schedule action")

    def effect(key: str) -> tuple[int, dict[str, Any]]:
        result: dict[str, Any] = {}
        if action == "enable":
            s.scheduler.enable(schedule_id)
        elif action == "pause":
            s.scheduler.pause(schedule_id, (body or {}).get("reason", "paused by a person"))
        else:
            # A person asked for this run, so the run records the person (with the watcher id).
            origin = StartedBy(kind=StartedByKind.PERSON, role=principal.role, watcher_id=schedule_id)
            r = s.scheduler.tick(only=schedule_id, run_once=True, started_by=origin)
            result = {"admitted": r.admitted, "duplicates": r.duplicates, "held": r.held, "skipped": r.skipped}
            s.run_in_background(s.dispatcher.dispatch_pending)
        s.audit.record(principal.role.value, f"schedule.{action}", schedule_id, body or {})
        return 200, envelope(request, principal, {"schedule": _schedule_view(s, schedule_id), **result})

    return idempotent(request, principal, body or {}, effect)


# ------------------------------------------------------------------ snapshots

@router.post("/ingest", status_code=202)
def ingest(request: Request, body: dict[str, Any] = Body(...)):
    principal = principal_of(request)
    require_role(principal, {"em", "audit"}, "start live ingestion")
    s = services_of(request)
    if s.mode.get().value == "offline":
        raise ApiError(403, "offline", "live ingestion is not possible in Offline mode")

    def effect(key: str) -> tuple[int, dict[str, Any]]:
        out = dict(s.snapshots.ingest(list(body.get("sources", [])), _project(principal, s, body)))
        s.audit.record(principal.role.value, "ingest", out.get("ingest_id", ""), body)
        return 202, envelope(request, principal, out)

    return idempotent(request, principal, body, effect)


@router.post("/snapshots", status_code=201)
def freeze(request: Request, body: dict[str, Any] = Body(...)):
    principal = principal_of(request)
    require_role(principal, {"em", "audit"}, "freeze a snapshot")
    s = services_of(request)

    def effect(key: str) -> tuple[int, dict[str, Any]]:
        out = dict(s.snapshots.freeze(_project(principal, s, body)))
        s.audit.record(principal.role.value, "snapshot.freeze", out.get("snapshot_id", ""), body)
        return 201, envelope(request, principal, out)

    return idempotent(request, principal, body, effect)


@router.get("/snapshots")
def snapshots(request: Request, release: str | None = Query(None)) -> dict[str, Any]:
    principal = principal_of(request)
    pid = None if principal.project_id == ANY_PROJECT else principal.project_id
    s = services_of(request)
    items = [dict(x) for x in s.snapshots.list(release=release, project_id=pid)]
    names = {}
    projects = getattr(s, "projects", None)
    for it in items:
        p_id = it.get("project_id")
        if p_id and p_id not in names:
            cfg = projects.get(p_id) if projects is not None and hasattr(projects, "get") else None
            names[p_id] = getattr(cfg, "name", None) or str(p_id).replace("-", " ").title()
    items = [describe(s.snapshots, it, names.get(it.get("project_id"))) for it in items]
    return envelope(request, principal, {"snapshots": items})


@router.get("/snapshots/{snapshot_id}")
def snapshot(request: Request, snapshot_id: str) -> dict[str, Any]:
    principal = principal_of(request)
    return envelope(request, principal, {"snapshot": _snapshot(request, principal, snapshot_id)})


@router.get("/graph/{snapshot_id}")
def graph(request: Request, snapshot_id: str) -> dict[str, Any]:
    principal = principal_of(request)
    _snapshot(request, principal, snapshot_id)
    g = graph_view(services_of(request).snapshots.graph(snapshot_id))
    return envelope(request, principal, {"snapshot_id": snapshot_id, **g})


def _short_label(node_id: str) -> str:
    """A readable name for a node: the part after the kind prefix."""
    return node_id.split(":", 1)[1] if ":" in node_id else node_id


def graph_view(raw: Any) -> dict[str, Any]:
    """One graph shape for every client.

    Nodes carry ``kind`` and ``label`` (as well as the stored ``type``); edges carry ``source``
    and ``target`` (as well as the stored ``from`` and ``to``), so drawing code never has to guess.
    """
    g = dict(raw or {})
    nodes = []
    for n in g.get("nodes", []) or []:
        node = dict(n)
        node.setdefault("kind", node.get("type", "node"))
        node.setdefault("type", node["kind"])
        node.setdefault("label", _short_label(str(node.get("id", ""))))
        nodes.append(node)
    edges = []
    for e in g.get("edges", []) or []:
        edge = dict(e)
        edge.setdefault("source", edge.get("from"))
        edge.setdefault("target", edge.get("to"))
        edge.setdefault("from", edge["source"])
        edge.setdefault("to", edge["target"])
        edge.setdefault("kind", "link")
        edges.append(edge)
    g["nodes"] = nodes
    g["edges"] = edges
    return g


# ------------------------------------------------------------------ runs and reports

@router.get("/runs")
def runs(request: Request) -> dict[str, Any]:
    principal = principal_of(request)
    s = services_of(request)
    from atlas.scheduler import ADMITTED, DISPATCHED, DISPATCHING, HELD

    items = [run_view(request, a.run_id) for a in s.admissions.by_status(ADMITTED, HELD, DISPATCHING, DISPATCHED)
             if principal.project_id == ANY_PROJECT or (a.project_id or None) in (None, principal.project_id)]
    return envelope(request, principal, {"runs": [i for i in items if i is not None]})


@router.get("/runs/{run_id}")
def run(request: Request, run_id: str) -> dict[str, Any]:
    principal = principal_of(request)
    view = check_project(request, principal, run_view(request, run_id), "run")
    return envelope(request, principal, dict(view))


def _report(request: Request, principal: Any, report_id: str, generation: int | None) -> dict[str, Any]:
    s = services_of(request)
    if ":" not in report_id:
        raise ApiError(404, "not_found", "report not found")
    run_id, rtype = report_id.rsplit(":", 1)
    check_project(request, principal, run_view(request, run_id), "report")
    if rtype not in REPORT_TYPES or s.orchestrator is None:
        raise ApiError(404, "not_found", "report not found")
    auth = s.orchestrator.runs.authoritative(run_id)
    if auth is None:
        raise ApiError(404, "not_found", "report not found")
    gen, artefact = auth
    superseded_by = None
    if generation is not None and generation != gen:
        older = s.orchestrator.runs.generation(run_id, generation)
        if older is None:
            raise ApiError(404, "not_found", "no such generation")
        artefact, superseded_by = older, gen
    rendered = render(rtype, artefact, principal, s.projection)
    body = {"report_id": report_id, "run_id": run_id, "report_type": rtype,
            "authoritative_generation": gen, "generation": artefact.get("authoritative_generation", gen),
            "superseded_by": superseded_by, "report_version": rendered["report_version"],
            "correction_version": rendered["correction_version"], "corrections": artefact.get("corrections", []),
            "markdown": rendered["markdown"], "report": rendered["json"],
            "withheld": rendered["json"].get("restricted_items", 0)}
    return envelope(request, principal, body, data_mode=rendered["json"].get("data_mode", "REAL"))


@router.get("/reports")
def reports(request: Request, run_id: str = Query(...), type: str | None = Query(None)) -> dict[str, Any]:
    principal = principal_of(request)
    view = check_project(request, principal, run_view(request, run_id), "run")
    items = [r for r in view["reports"] if type is None or r["report_type"] == type]
    for r in items:
        r["superseded"] = False
    return envelope(request, principal, {"run_id": run_id, "reports": items})


@router.get("/reports/{report_id}")
def report(request: Request, report_id: str, generation: int | None = Query(None)) -> dict[str, Any]:
    from atlas.api import access_control as ac

    principal = principal_of(request)
    _, reader, grant = ac.reader_for(request, principal, "report", report_id)
    body = _report(request, reader, report_id, generation)
    body["read_only"] = grant is not None
    return body


@router.get("/evidence/{record_id:path}")
def evidence(request: Request, record_id: str, run_id: str = Query(...),
             content_version_id: str | None = Query(None)) -> dict[str, Any]:
    principal = principal_of(request)
    s = services_of(request)
    view = check_project(request, principal, run_view(request, run_id), "run")
    rec = s.snapshots.evidence(view["snapshot_id"], record_id, content_version_id, ())
    if rec is None:
        raise ApiError(404, "not_in_run", "this record is not part of the run's snapshot or lineage")
    rec = dict(rec)
    rec.setdefault("required_scopes", list(rec.get("access_scopes", ["internal"])))
    return envelope(request, principal, {"run_id": run_id, "record": rec},
                    data_mode="SYNTHETIC" if rec.get("provenance") == "synthetic" else "REAL")


@router.get("/queue")
def queue(request: Request) -> dict[str, Any]:
    principal = principal_of(request)
    s = services_of(request)
    return envelope(request, principal, {"entries": s.queue.view(s.projection, principal)})


@router.post("/feedback", status_code=201)
def feedback(request: Request, body: dict[str, Any] = Body(...)):
    principal = principal_of(request)
    s = services_of(request)
    check_project(request, principal, run_view(request, str(body.get("run_id", ""))), "run")

    def effect(key: str) -> tuple[int, dict[str, Any]]:
        try:
            rec = s.feedback.record_feedback(body["run_id"], body.get("kind", ""), str(body.get("item_id", "")),
                                             str(body.get("decision", "")), str(body.get("rationale", "")),
                                             principal.role.value)
        except (ValueError, KeyError) as exc:
            raise ApiError(400, "invalid_feedback", str(exc)) from exc
        if rec["golden_candidate"] and s.candidates_dir is not None:
            s.candidates_dir.mkdir(parents=True, exist_ok=True)
            (s.candidates_dir / f"{rec['id']}.json").write_text(json.dumps(rec, indent=2), encoding="utf-8")
        return 201, envelope(request, principal, {"feedback": dict(rec, required_scopes=["internal"])})

    return idempotent(request, principal, body, effect)


__all__ = ["router", "run_view", "Refused"]
