"""Evaluations, the demo manifest, and the scenario-only routes."""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, Body, Request

from atlas.api.identity import principal_of, require_role
from atlas.api.support import ApiError, envelope, idempotent, services_of

router = APIRouter()
scenario_router = APIRouter()


def _evaluation_view(ev: dict[str, Any]) -> dict[str, Any]:
    """Evidence excerpts inside an evaluation keep their scopes, so projection withholds them per reader."""
    out = dict(ev)
    out.setdefault("required_scopes", ["internal"])
    gates = out.get("gates")
    if isinstance(gates, dict):
        out["gates_by_id"] = gates
        out["gates"] = [_gate_view(gid, g) for gid, g in gates.items()]
        limits = [g.get("limitation") for g in gates.values() if isinstance(g, dict) and g.get("limitation")]
        if limits:
            out.setdefault("proxy_limitation", limits[0])
    return out


def _gate_view(gate_id: str, g: Any) -> dict[str, Any]:
    g = dict(g) if isinstance(g, dict) else {"status": str(g)}
    view = {**g, "gate_id": gate_id, "status": g.get("status", "unevaluated")}
    parts = [str(f) for f in g.get("failures") or []]
    if g.get("note"):
        parts.append(str(g["note"]))
    if g.get("checked_by"):
        parts.append(f"Checked by {g['checked_by']}")
    if parts:
        view["summary"] = "; ".join(parts)
    if g.get("limitation"):
        view["proxy_note"] = g["limitation"]
    if isinstance(g.get("per_close"), list):
        view["per_close"] = [{**c, "close": c.get("close", c.get("release"))} for c in g["per_close"]]
    if "pooled" not in view and isinstance(g.get("counts"), dict) and "recall" in g["counts"]:
        view["pooled"] = g["counts"]
    return view


@router.get("/evaluations")
def evaluations(request: Request) -> dict[str, Any]:
    principal = principal_of(request)
    return envelope(request, principal, {"evaluations": services_of(request).evaluations.list()})


@router.get("/evaluations/latest")
def latest_evaluation(request: Request) -> dict[str, Any]:
    principal = principal_of(request)
    ev = services_of(request).evaluations.latest()
    if ev is None:
        raise ApiError(404, "not_found", "no completed evaluation yet")
    return envelope(request, principal, {"evaluation": _evaluation_view(ev)})


@router.get("/evaluations/{evaluation_id}")
def evaluation(request: Request, evaluation_id: str) -> dict[str, Any]:
    principal = principal_of(request)
    ev = services_of(request).evaluations.get(evaluation_id)
    if ev is None:
        raise ApiError(404, "not_found", "evaluation not found")
    return envelope(request, principal, {"evaluation": _evaluation_view(ev)})


@router.get("/demo/manifest")
def demo_manifest(request: Request) -> dict[str, Any]:
    principal = principal_of(request)
    s = services_of(request)
    manifest = json.loads(s.manifest_path.read_text(encoding="utf-8"))
    return envelope(request, principal, {"manifest": manifest, "variables": dict(s.demo_variables),
                                         "this_server": s.environment})


# ------------------------------------------------------------------ scenario server only

def _scenario_effect(request: Request, kind: str, body: dict[str, Any]):
    principal = principal_of(request)
    require_role(principal, {"audit"}, "use scenario controls")
    s = services_of(request)
    if s.scenario is None:  # defensive: the routes are never mounted on the normal server
        raise ApiError(404, "not_found", "not found")

    def effect(key: str) -> tuple[int, dict[str, Any]]:
        scenario_id = f"scn-{uuid.uuid4().hex[:10]}"
        record = dict(body, scenario_id=scenario_id, provenance="synthetic")
        if kind == "events":
            s.scenario.events.append(record)
        elif kind == "faults":
            s.scenario.faults[str(body.get("fault", ""))] = record
            if body.get("fault") == "containment_trip":
                trip = getattr(s.containment, "trip", None)
                if trip is None:
                    raise ApiError(503, "not_connected", "the containment service cannot be tripped on this server")
                record["incident_id"] = trip(str(body.get("switch", "model_calls")), "scenario fault")
        elif kind == "clock":
            s.scenario.clock_offset_hours += float(body.get("advance_hours", 0))
        elif kind == "spend":
            s.scenario.spend_usd = float(body.get("usd", 0))
            if s.spend is not None:
                s.spend.add(max(0.0, s.scenario.spend_usd - s.spend.spent))
        s.audit.record(principal.role.value, f"scenario.{kind}", scenario_id, body)
        return 201, envelope(request, principal, {"scenario": record}, data_mode="SYNTHETIC")

    return idempotent(request, principal, body, effect)


@scenario_router.post("/scenario/events", status_code=201)
def scenario_events(request: Request, body: dict[str, Any] = Body(...)):
    return _scenario_effect(request, "events", body)


@scenario_router.post("/scenario/faults", status_code=201)
def scenario_faults(request: Request, body: dict[str, Any] = Body(...)):
    return _scenario_effect(request, "faults", body)


@scenario_router.post("/scenario/clock", status_code=201)
def scenario_clock(request: Request, body: dict[str, Any] = Body(...)):
    return _scenario_effect(request, "clock", body)


@scenario_router.post("/scenario/spend", status_code=201)
def scenario_spend(request: Request, body: dict[str, Any] = Body(...)):
    return _scenario_effect(request, "spend", body)
