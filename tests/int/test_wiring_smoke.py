"""End-to-end smoke test of the wired server, in-process, with recorded model responses.

The server is built exactly as ``python -m atlas.serve`` builds it (real run engine, execution
layer, containment, notifications), with three test-only substitutions: a recorded model provider
instead of a live model, a scanner that records a clean scan for the task, and in-memory storage.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from atlas.api.app import create_app
from atlas.api.services import build_core
from atlas.config import load_settings
from atlas.contracts import ModelResult, Provenance, Record, SecurityContext
from atlas.wiring import connect
from tests.c.fakes import FIX_DIFF

BASE = "http://127.0.0.1:8000"


def H(role: str, key: str | None = None) -> dict[str, str]:
    h = {"X-Atlas-Role": role}
    if key:
        h["Idempotency-Key"] = str(uuid.uuid5(uuid.NAMESPACE_URL, key))
    return h


class RecordedProvider:
    """Recorded answers per model role; never reaches a real model."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def complete(self, role, messages, schema, limits):
        self.calls.append(role)
        content = {"coding_agent": {"diff": FIX_DIFF},
                   "verifier": {"passed": True, "reason": "the diff wraps IPv6 hosts in brackets as asked"}}.get(role)
        if content is None:
            return ModelResult({}, "failed", 0, 0, "recorded", "recorded")
        return ModelResult(content, "complete", 10, 10, "recorded", "recorded")


class CleanScanner:
    """Test scanner: writes one successful, empty scan for the task's revision."""

    def __init__(self, partition) -> None:
        self.partition = partition

    def scan(self, ctx: SecurityContext) -> str:
        sid = f"scan-{uuid.uuid4().hex[:6]}"
        now = datetime.now(timezone.utc)
        content = json.dumps({"kind": "scan_run", "scan_run_id": sid, "coverage": ["target"], "status": "success",
                              "finding_ids": [], "target_revision": ctx.scanned_revision,
                              "observed_at": now.isoformat()})
        self.partition.write(ctx, Record(
            record_id=sid, source="security", source_id=sid, revision=None, content_version_id=sid,
            valid_from=now, observed_at=now, confirmed_through=now, writer="security_connector",
            provenance=Provenance.OBSERVED, synthetic_ancestry=False, access_scopes=frozenset({"internal"}),
            lineage=(), content_ref=content))
        return sid


def _server(tmp_path):
    settings = load_settings(None, environ={"ATLAS_DATA_DIR": str(tmp_path), "ATLAS_PUBLISH_TARGET": "example/target",
                                            "ATLAS_DEFAULT_PROJECT": "example", "ATLAS_SANDBOX_RUNNER": "none"})
    services = build_core()
    services.default_project_id = "example"
    provider = RecordedProvider()
    wiring = connect(services, settings, env={"ATLAS_SANDBOX_RUNNER": "none"}, provider=provider, persist=False)
    wiring.scanner = CleanScanner(wiring.security_partition)
    services.model_configured = True
    return services, wiring, provider, TestClient(create_app(services), base_url=BASE)


def _delegated_run(services, wiring, snapshot_id: str) -> str:
    """A verified run whose authoritative result delegates a task (test setup for the task path).

    The run is admitted the normal way; its verified result is written directly instead of asking a
    model, so the task path can be exercised without live calls.
    """
    from atlas.contracts import Role, RunContext, RunMode, StartedBy, StartedByKind

    adm = services.admissions.admit(f"test:{uuid.uuid4().hex}", {"trigger": "failure", "event_id": "e1"}, snapshot_id,
                                    StartedBy(kind=StartedByKind.PERSON, role=Role.ONCALL))
    run_id = adm.run_id
    now = datetime.now(timezone.utc)
    ctx = RunContext(run_id=run_id, snapshot_id=snapshot_id, mode=RunMode.REPLAY, release_or_case="example",
                     cutoff=now, alias_variant=None, evidence_clock=now)
    runs = wiring.orchestrator.runs
    runs.save_run(run_id, ctx, {"trigger": "failure", "event_id": "e1"}, {"kind": "person", "role": "oncall"}, {})
    runs.write_generation(run_id, {"run_id": run_id, "action": {"value": "DELEGATE_TO_CODING_AGENT", "claim_ids": ["c1"]},
                                   "classification": {"value": "TEST_DEFECT"},
                                   "root_cause": {"text": "JoinHostPort does not bracket IPv6 hosts"},
                                   "injection_detected": False})
    return run_id


def test_health_catalog_question_notifications_and_task_path(tmp_path) -> None:
    services, wiring, provider, client = _server(tmp_path)

    health = client.get("/api/health", headers=H("pm"))
    assert health.status_code == 200

    catalog = client.get("/api/triggers/catalog", headers=H("em"))
    assert catalog.status_code == 200 and catalog.json()

    snap = client.post("/api/snapshots", json={"project_id": "example"}, headers=H("em", "k-snap"))
    assert snap.status_code in (200, 201), snap.text
    snapshot_id = snap.json()["snapshot"]["snapshot_id"] if "snapshot" in snap.json() else snap.json()["snapshot_id"]

    question = "Which developers caused the most flaky tests this release? Rank them for performance reviews."
    r = client.post("/api/triggers/question", json={"snapshot_id": snapshot_id, "question": question},
                    headers=H("em", "k-question"))
    assert r.status_code == 202, r.text
    run_id = r.json()["run_id"]
    report = client.get(f"/api/reports/{run_id}:question", headers=H("em")).json()["report"]
    assert report["classification"]["value"] == "OUT_OF_SCOPE" and report["action"]["value"] == "REJECT"
    assert provider.calls == []  # refused in code, before any model call

    notes = client.get("/api/notifications", headers=H("em"))
    assert notes.status_code == 200

    run_id = _delegated_run(services, wiring, snapshot_id)
    created = client.post("/api/tasks", json={"run_id": run_id}, headers=H("em", "k-task"))
    assert created.status_code == 201, created.text
    task = created.json()["task"]
    assert task["state"] == "proposed" and task["security"]["state"] == "passed"

    pending_a = next(a for a in client.get("/api/approvals", headers=H("em")).json()["approvals"]
                     if a["task_id"] == task["task_id"] and a["kind"] == "A")
    a = client.post(f"/api/approvals/{pending_a['approval_id']}/decision", json={"decision": "approve", "review_minutes": 3},
                    headers=H("em", "k-a"))
    assert a.status_code == 200, a.text
    assert client.get(f"/api/tasks/{task['task_id']}", headers=H("em")).json()["task"]["state"] == "approved_a"

    prepared = client.post(f"/api/tasks/{task['task_id']}/prepare", json={}, headers=H("em", "k-prep"))
    assert prepared.status_code in (200, 201), prepared.text
    assert "coding_agent" in provider.calls

    pending_b = next(a for a in client.get("/api/approvals", headers=H("em")).json()["approvals"]
                     if a["task_id"] == task["task_id"] and a["kind"] == "B")
    assert pending_b["diff_hash"] and pending_b["destination_repository"] == "example/target"
    assert pending_b["verification_generation"] == 1 and pending_b["security_valid_until"]
    b = client.post(f"/api/approvals/{pending_b['approval_id']}/decision", json={"decision": "approve", "review_minutes": 3},
                    headers=H("em", "k-b"))
    assert b.status_code == 409, b.text
    assert "unavailable" in b.text or "not run" in b.text or "container" in b.text


def test_without_a_scan_the_task_cannot_get_approval_a(tmp_path) -> None:
    settings = load_settings(None, environ={"ATLAS_DATA_DIR": str(tmp_path), "ATLAS_PUBLISH_TARGET": "example/target",
                                            "ATLAS_SANDBOX_RUNNER": "none"})
    services = build_core()
    wiring = connect(services, settings, env={"ATLAS_SANDBOX_RUNNER": "none"}, provider=RecordedProvider(), persist=False)
    client = TestClient(create_app(services), base_url=BASE)
    snapshot_id = wiring.snapshots.freeze("example")["snapshot_id"]
    run_id = _delegated_run(services, wiring, snapshot_id)
    task = client.post("/api/tasks", json={"run_id": run_id}, headers=H("em", "k1")).json()["task"]
    assert task["security"]["state"] == "not_run"
    pending = next(a for a in client.get("/api/approvals", headers=H("em")).json()["approvals"] if a["task_id"] == task["task_id"])
    r = client.post(f"/api/approvals/{pending['approval_id']}/decision", json={"decision": "approve", "review_minutes": 1},
                    headers=H("em", "k2"))
    assert r.status_code == 409 and "security" in r.text


def test_unconnected_parts_answer_not_connected(tmp_path) -> None:
    settings = load_settings(None, environ={"ATLAS_DATA_DIR": str(tmp_path)})
    services = build_core()
    connect(services, settings, env={}, provider=RecordedProvider(), persist=False)
    client = TestClient(create_app(services), base_url=BASE)
    r = client.post("/api/ingest", json={"project_id": "example", "sources": ["issues"]}, headers=H("em", "k-ing"))
    assert r.status_code == 503 and "not connected" in r.json()["reason"]


def test_offline_outbound_attempt_trips_containment(tmp_path) -> None:
    from atlas.contracts import EgressPurpose, Mode

    settings = load_settings(None, environ={"ATLAS_DATA_DIR": str(tmp_path)})
    services = build_core(mode=Mode.OFFLINE)
    wiring = connect(services, settings, env={}, provider=RecordedProvider(), persist=False)
    try:
        wiring.outbound.call("investigator", EgressPurpose.MODEL_CALL, {"messages": [{"role": "user", "content": "x"}]})
    except Exception:
        pass
    assert not wiring.containment.permits("model_calls").allowed
    state = services.containment.state()
    assert any(i["row"] == 4 for i in state["incidents"])
