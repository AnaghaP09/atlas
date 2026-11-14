"""Fixtures for the HTTP API: an app wired to fakes for the data and execution layers, and a stub
run engine that writes verified results through the real run store."""

from __future__ import annotations

from typing import Any, Mapping

import pytest
from fastapi.testclient import TestClient

from atlas.api.app import create_app
from atlas.api.services import build_core
from atlas.orchestrator import RunStore
from tests.d.fakes import FakeContainmentPort, FakeSnapshots, FakeTasks

BASE = "http://127.0.0.1:8000"


def artefact(run_id: str, report_type: str = "investigation", action: str = "ASSIGN_DEVELOPER",
             claims: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "run_id": run_id, "report_type": report_type, "report_version": 1, "correction_version": 0,
        "run_status": "complete", "data_mode": "REAL", "required_scopes": ["public"],
        "classification": {"value": "CODE_DEFECT", "claim_ids": ["c1"], "required_scopes": ["public"]},
        "action": {"value": action, "claim_ids": ["c1"], "required_scopes": ["public"]},
        "findings": [{"text": "public finding", "claim_ids": ["c1"], "required_scopes": ["public"]},
                     {"text": "SECRET-FINDING-MARKER", "claim_ids": ["c2"], "required_scopes": ["restricted:security"]}],
        "claims": claims if claims is not None else [
            {"id": "c1", "text": "virt-handler race", "evidence_ids": ["rec-public"], "required_scopes": ["public"],
             "verdict": "supported"}],
        "security_check": {"state": "passed"}, "cost": {"steps": 3, "tool_calls": 4},
    }


class StubOrchestrator:
    """Writes a prepared result for each run through the real run store and emits real events."""

    def __init__(self, services: Any) -> None:
        self.services = services
        self.runs = RunStore()
        self.action = "ASSIGN_DEVELOPER"
        self.started: list[str] = []

    def run(self, run_id: str, ctx: Any, trigger: Mapping[str, Any], started_by: Any) -> dict[str, Any]:
        self.started.append(run_id)
        rtype = {"release_close": "forecast", "question": "question"}.get(trigger.get("trigger"), "investigation")
        out = artefact(run_id, rtype, self.action)
        self.runs.save_run(run_id, ctx, trigger, {"kind": str(started_by.kind)}, out)
        gen = self.runs.write_generation(run_id, out)
        self.services.events.emit("run.status_changed", run_id=run_id, authoritative_generation=gen,
                                  payload={"status": "complete"}, required_scopes=["public"])
        return out

    def reverify(self, run_id: str) -> dict[str, Any]:
        n, art = self.runs.authoritative(run_id)
        new = dict(art, report_version=art.get("report_version", 1) + 1,
                   findings=[f for f in art["findings"] if f["text"] != "public finding"])
        gen = self.runs.write_generation(run_id, new)
        tasks = self.services.tasks
        assessments = {t["task_id"]: "still_supported" for t in tasks.tasks_for_run(run_id)}
        tasks.apply_reverification(run_id, gen, assessments)
        self.services.events.emit("run.generation_changed", run_id=run_id, authoritative_generation=gen,
                                  payload={"previous_generation": n}, required_scopes=["public"])
        self.services.events.emit("report.superseded", run_id=run_id, authoritative_generation=gen,
                                  report_version=new["report_version"], payload={"superseded_generation": n},
                                  required_scopes=["public"])
        return dict(new, authoritative_generation=gen)


@pytest.fixture
def services():
    s = build_core()
    s.snapshots = FakeSnapshots()
    s.tasks = FakeTasks()
    s.containment = FakeContainmentPort()
    s.model_configured = True
    s.orchestrator = StubOrchestrator(s)
    return s


@pytest.fixture
def app(services):
    return create_app(services)


@pytest.fixture
def client(app):
    return TestClient(app, base_url=BASE)


def H(role: str = "em", key: str | None = None, **extra: str) -> dict[str, str]:
    import uuid

    h = {"X-Atlas-Role": role, "Idempotency-Key": key or str(uuid.uuid4())}
    h.update(extra)
    return h
