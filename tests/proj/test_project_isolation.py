"""Project separation: one project's people never see or change another project's objects.

Two projects are configured (``kubevirt`` and ``argo-cd``). Every check here is about the object's
STORED project, never a project named in a request body.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from atlas.api.app import create_app
from atlas.api.identity import HeaderIdentity
from atlas.api.services import build_core
from atlas.api.streams import NOTIFICATION_STREAM, PROJECT_STREAM
from atlas.contracts import StartedBy, StartedByKind
from atlas.feed import ProjectFeed
from atlas.hitl import HumanQueue
from atlas.projection import AuthorizationProjection, principal_for
from atlas.projects import ProjectRegistry
from atlas.scheduler import AdmissionStore, ObservedEvent, Scheduler, Watcher
from tests.c.fakes import make_env
from tests.d.conftest import BASE, StubOrchestrator
from tests.d.fakes import CUTOFF, FakeContainmentPort, FakeSnapshots, FakeTasks

REGISTRY = ProjectRegistry.from_mapping({
    "kubevirt": {"name": "KubeVirt", "export": {"publish_targets": ["owner/sandbox"], "notification_audiences": ["em"]}},
    "argo-cd": {"name": "Argo CD", "export": {"publish_targets": [], "notification_audiences": []}},
})
PERSON = StartedBy(kind=StartedByKind.PERSON)


def H(role: str, key: str | None = None) -> dict[str, str]:
    h = {"X-Atlas-Role": role}
    if key:
        h["Idempotency-Key"] = key
    return h


@pytest.fixture
def services():
    s = build_core(identity=HeaderIdentity(projects=REGISTRY, default_project="kubevirt"))
    s.projects = REGISTRY
    s.default_project_id = "kubevirt"
    snaps = FakeSnapshots()
    snaps.snapshots["snap-argo"] = {"snapshot_id": "snap-argo", "project_id": "argo-cd", "release": "3.0",
                                    "kind": "prepared", "cutoff": CUTOFF, "alias_variant": None, "mode": "replay"}
    s.snapshots = snaps
    s.tasks = FakeTasks()
    s.containment = FakeContainmentPort()
    s.model_configured = True
    s.orchestrator = StubOrchestrator(s)
    return s


@pytest.fixture
def client(services):
    return TestClient(create_app(services), base_url=BASE)


def _investigate(client, project: str, snapshot: str, key: str | None = None):
    return client.post(f"/api/triggers/failure?project_id={project}", json={"snapshot_id": snapshot, "event_id": "G04"},
                       headers=H("em", key or str(uuid.uuid4())))


def _snapshot_state(services):
    return {k: dict(v) for k, v in services.tasks.tasks.items()}, {k: dict(v) for k, v in services.tasks.approval_rows.items()}


# ---------------------------------------------------------------- mutations across projects

def test_cross_project_mutations_change_nothing_and_call_nothing(client, services):
    run_id = _investigate(client, "kubevirt", "snap-v1.9.0").json()["run_id"]
    task = services.tasks.create(run_id, 1, {"value": "DELEGATE_TO_CODING_AGENT"}, "em", "k1")
    approval_id = next(iter(services.tasks.approval_rows))
    services.tasks.calls.clear()
    tasks_before, approvals_before = _snapshot_state(services)
    queue_before = services.queue.entries()
    admissions_before = len(services.admissions.for_project("kubevirt")) + len(services.admissions.for_project("argo-cd"))
    outbound_calls_before = len(services.orchestrator.started)

    attempts = [
        ("post", f"/api/approvals/{approval_id}/decision?project_id=argo-cd", {"decision": "approve"}),
        ("post", f"/api/tasks/{task['task_id']}/prepare?project_id=argo-cd", {}),
        ("post", f"/api/tasks/{task['task_id']}/publish?project_id=argo-cd", {}),
        ("post", f"/api/tasks/{task['task_id']}/security-refresh?project_id=argo-cd", {}),
        ("post", f"/api/tasks/{task['task_id']}/adjudicate?project_id=argo-cd", {"verdict": "correct"}),
        ("post", f"/api/runs/{run_id}/reverify?project_id=argo-cd", {}),
        ("post", "/api/tasks?project_id=argo-cd", {"run_id": run_id}),
        ("post", "/api/triggers/failure?project_id=argo-cd", {"snapshot_id": "snap-v1.9.0", "event_id": "G05"}),
    ]
    for method, path, body in attempts:
        r = getattr(client, method)(path, json=body, headers=H("em", str(uuid.uuid4())))
        assert r.status_code == 404, (path, r.status_code, r.text)

    assert _snapshot_state(services) == (tasks_before, approvals_before)
    assert services.tasks.calls == []
    assert services.queue.entries() == queue_before
    assert len(services.admissions.for_project("kubevirt")) + len(services.admissions.for_project("argo-cd")) == admissions_before
    assert len(services.orchestrator.started) == outbound_calls_before


def test_a_project_in_the_body_is_never_used(client, services):
    calls = []
    services.snapshots.ingest = lambda sources, project_id: calls.append(project_id) or {"ingest_id": "i1", "project_id": project_id}
    r = client.post("/api/ingest?project_id=kubevirt", json={"project_id": "argo-cd", "sources": ["issues"]},
                    headers=H("em", str(uuid.uuid4())))
    assert r.status_code == 202 and calls == ["kubevirt"]


def test_reads_of_another_projects_objects_answer_like_missing(client, services):
    run_id = _investigate(client, "kubevirt", "snap-v1.9.0").json()["run_id"]
    task = services.tasks.create(run_id, 1, {"value": "DELEGATE_TO_CODING_AGENT"}, "em", "k2")
    for path in (f"/api/runs/{run_id}", f"/api/tasks/{task['task_id']}", f"/api/runs/{run_id}/stream?follow=false",
                 "/api/snapshots/snap-v1.9.0", "/api/graph/snap-v1.9.0"):
        assert client.get(path + ("&" if "?" in path else "?") + "project_id=argo-cd", headers=H("pm")).status_code == 404, path
    listed = client.get("/api/runs?project_id=argo-cd", headers=H("pm")).json()["runs"]
    assert run_id not in [r["run_id"] for r in listed]
    assert client.get("/api/runs?project_id=no-such-project", headers=H("pm")).status_code == 404


# ---------------------------------------------------------------- admission and cursors

def test_the_same_source_event_in_two_projects_is_two_admissions():
    store = AdmissionStore()
    a = store.admit("issue-18234@1", {"trigger": "failure"}, "s1", PERSON, project_id="kubevirt")
    b = store.admit("issue-18234@1", {"trigger": "failure"}, "s2", PERSON, project_id="argo-cd")
    again = store.admit("issue-18234@1", {"trigger": "failure"}, "s1", PERSON, project_id="kubevirt")
    assert a.created and b.created and a.run_id != b.run_id
    assert not again.created and again.run_id == a.run_id
    assert a.event_key == b.event_key == "issue-18234@1"
    assert [x.run_id for x in store.for_project("argo-cd")] == [b.run_id]


def test_watcher_cursors_are_per_project_and_survive_restart(tmp_path):
    path = tmp_path / "admissions.sqlite"

    def watchers():
        ev = ObservedEvent(key="job-77@1", position="77", trigger={"trigger": "failure", "event_id": "job-77"},
                           snapshot_id="live")
        return [Watcher("kubevirt-failures", "failure", lambda cur: [] if cur == "77" else [ev], project_id="kubevirt"),
                Watcher("argo-failures", "failure", lambda cur: [] if cur == "77" else [ev], project_id="argo-cd")]

    first = Scheduler(AdmissionStore(path), watchers()).tick()
    assert len(first.admitted) == 2
    restarted = Scheduler(AdmissionStore(path), watchers()).tick()
    assert restarted.admitted == [] and restarted.duplicates == []
    reopened = AdmissionStore(path)
    assert len(reopened.for_project("kubevirt")) == 1 and len(reopened.for_project("argo-cd")) == 1


# ---------------------------------------------------------------- idempotency keys

def test_the_same_idempotency_key_in_two_projects_is_two_requests(client, services):
    key = str(uuid.uuid4())
    r1 = _investigate(client, "kubevirt", "snap-v1.9.0", key)
    r2 = _investigate(client, "argo-cd", "snap-argo", key)
    assert r1.status_code == r2.status_code == 202
    assert r1.json()["run_id"] != r2.json()["run_id"]
    replay = _investigate(client, "kubevirt", "snap-v1.9.0", key)
    assert replay.headers.get("Idempotent-Replay") == "true" and replay.json()["run_id"] == r1.json()["run_id"]


def test_task_store_keys_are_partitioned_by_project():
    env = make_env()
    common = dict(run_id="run-1", generation=1, action="DELEGATE_TO_CODING_AGENT", task_class="c", task_text="t",
                  scope=["a.go"], destination_repo="owner/sandbox", destination_node_id="n", base_ref="main",
                  base_commit="b", snapshot_id="s", policy_version="p", scanner=env.scanner, gate=env.gate, now=env.now,
                  idempotency_key="same-key")
    k = env.tasks.create(**common, project_id="kubevirt")
    a = env.tasks.create(**common, project_id="argo-cd")
    again = env.tasks.create(**common, project_id="kubevirt")
    assert k.task_id != a.task_id and again.task_id == k.task_id
    assert (k.project_id, a.project_id) == ("kubevirt", "argo-cd")
    assert [t.task_id for t in env.tasks.for_project("argo-cd")] == [a.task_id]
    assert env.tasks.view(k.task_id)["project_id"] == "kubevirt"


# ---------------------------------------------------------------- projection on every surface

def test_projection_hides_other_projects_objects():
    projection = AuthorizationProjection()
    artefact = {"required_scopes": [], "items": [
        {"text": "mine", "project_id": "kubevirt", "required_scopes": ["internal"]},
        {"text": "theirs", "project_id": "argo-cd", "required_scopes": ["internal"]},
        {"text": "installation-wide", "required_scopes": ["internal"]}]}
    out = projection.project(artefact, principal_for("pm", project_id="kubevirt"))
    assert [i["text"] for i in out["items"]] == ["mine", "installation-wide"]
    whole = projection.project({"required_scopes": [], "project_id": "argo-cd", "x": 1}, principal_for("audit", project_id="kubevirt"))
    assert whole.get("withheld") is True and "x" not in whole


def test_notifications_queue_and_streams_stay_in_their_project(services):
    services.notifier.emit("run.failed", "run-k", ["run-k"], frozenset({"internal"}),
                           {"text": "kubevirt run failed", "required_scopes": ["internal"]}, project_id="kubevirt")
    services.notifier.emit("run.failed", "run-a", ["run-a"], frozenset({"internal"}),
                           {"text": "argo run failed", "required_scopes": ["internal"]}, project_id="argo-cd")
    em_k, em_a = principal_for("em", project_id="kubevirt"), principal_for("em", project_id="argo-cd")
    assert [n["subject_id"] for n in services.notifier.in_app(em_k)] == ["run-k"]
    assert [n["subject_id"] for n in services.notifier.in_app(em_a)] == ["run-a"]
    _, events_k = services.hub.read(NOTIFICATION_STREAM, em_k)
    assert {e["data"]["subject_id"] for e in events_k} == {"run-k"}

    queue = HumanQueue()
    queue.add("low_confidence", "run-k", "run-k", "low", project_id="kubevirt")
    queue.add("low_confidence", "run-a", "run-a", "low", project_id="argo-cd")
    seen = queue.view(AuthorizationProjection(), em_a)
    assert [e["run_id"] for e in seen] == ["run-a"]


def test_external_delivery_needs_the_projects_export_grant():
    assert REGISTRY.may_notify("kubevirt", "em") and not REGISTRY.may_notify("argo-cd", "em")
    assert REGISTRY.may_notify(None, "em")
    assert REGISTRY.may_publish("kubevirt", "owner/sandbox") and not REGISTRY.may_publish("argo-cd", "owner/sandbox")
    assert ProjectRegistry().may_publish("anything", "owner/sandbox")  # no projects file: installation-wide target


# ---------------------------------------------------------------- dashboard, activity and evidence

def test_dashboard_activity_and_project_stream(client, services):
    services.feed = ProjectFeed(services.hub, interval_seconds=60)
    services.feed.record_poll("kubevirt", datetime(2026, 11, 14, 10, 0, tzinfo=timezone.utc))
    services.feed.record_activity("kubevirt", {"kind": "issue", "title": "New flaky test", "record_id": "rec-1",
                                               "content_version_id": "cv1", "source_revision": "r1"})
    version = services.feed.update_panels("kubevirt", {"open_issues": [{"title": "New flaky test", "project_id": "kubevirt"}]})
    services.feed.record_activity("argo-cd", {"kind": "issue", "title": "Argo item", "record_id": "rec-9"})

    board = client.get("/api/projects/kubevirt/dashboard", headers=H("pm")).json()
    assert board["dashboard_version"] == version and board["interval_seconds"] == 60
    assert board["last_poll"].startswith("2026-11-14T10:00")
    panel = next(p for p in board["panels"] if p["panel_id"] == "open_issues")
    assert panel["status"] == "live" and panel["items"][0]["title"] == "New flaky test"

    activity = client.get("/api/projects/kubevirt/activity", headers=H("pm")).json()["activity"]
    assert [a["title"] for a in activity] == ["New flaky test"]
    assert activity[0]["link"] == "/api/projects/kubevirt/evidence/rec-1?content_version_id=cv1"

    body = client.get("/api/projects/kubevirt/stream?follow=false", headers=H("pm")).text
    assert "project.activity" in body and "project.dashboard_changed" in body and "Argo item" not in body
    assert "Argo item" in client.get("/api/projects/argo-cd/stream?follow=false", headers=H("pm")).text
    assert client.get("/api/projects/unknown/dashboard", headers=H("pm")).status_code == 404


def test_activity_evidence_is_project_bound_and_version_bound(client, services):
    assert client.get("/api/projects/kubevirt/evidence/rec-1", headers=H("pm")).status_code == 503
    versions = {("kubevirt", "rec-1", "cv1"): {"record_id": "rec-1", "content_version_id": "cv1", "text": "old",
                                               "project_id": "kubevirt", "access_scopes": ["internal"]},
                ("kubevirt", "rec-1", "cv2"): {"record_id": "rec-1", "content_version_id": "cv2", "text": "new",
                                               "project_id": "kubevirt", "access_scopes": ["internal"]}}
    services.snapshots.live_evidence = lambda p, r, v: versions.get((p, r, v))
    r = client.get("/api/projects/kubevirt/evidence/rec-1?content_version_id=cv1", headers=H("pm"))
    assert r.status_code == 200 and r.json()["record"]["text"] == "old"
    assert client.get("/api/projects/argo-cd/evidence/rec-1?content_version_id=cv1", headers=H("pm")).status_code == 404


def test_project_list_and_switching(client):
    data = client.get("/api/projects", headers=H("pm")).json()
    assert {p["id"] for p in data["projects"]} == {"kubevirt", "argo-cd"}
    assert data["current"] == "kubevirt"
    assert client.get("/api/projects?project_id=argo-cd", headers=H("pm")).json()["current"] == "argo-cd"


def test_stream_events_of_other_projects_never_reach_a_reader(services):
    feed = ProjectFeed(services.hub)
    feed.record_activity("argo-cd", {"kind": "issue", "title": "theirs"})
    _, events = services.hub.read(PROJECT_STREAM, principal_for("pm", project_id="kubevirt"))
    assert events == []
