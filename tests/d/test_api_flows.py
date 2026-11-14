"""Triggers, idempotency, runs, reports, re-verification, schedules and the catalog."""

from __future__ import annotations

import uuid

from atlas.scheduler import ObservedEvent, Watcher
from tests.d.conftest import H


def _investigate(client, role="em", key=None, event="G04"):
    return client.post("/api/triggers/manual/investigate", json={"snapshot_id": "snap-v1.9.0", "event_id": event},
                       headers=H(role, key))


def test_catalog_lists_every_manual_trigger_with_parameters_and_roles(client):
    body = client.get("/api/triggers/catalog", headers=H("em")).json()
    ids = {t["trigger_id"] for t in body["manual"]}
    assert ids == {"release_close", "investigate", "nightly", "question", "reverify"}
    rc = next(t for t in body["manual"] if t["trigger_id"] == "release_close")
    assert rc["allowed_roles"] == ["audit", "em", "pm"]
    assert {"name": "snapshot_id", "type": "string", "required": True} in rc["parameters"]
    assert all(t["availability"] == "available" for t in body["manual"])


def test_catalog_and_admission_share_one_table(client, services):
    services.containment.trip("model_calls", "acted-on injection")
    cat = client.get("/api/triggers/catalog", headers=H("em")).json()["manual"]
    inv = next(t for t in cat if t["trigger_id"] == "investigate")
    assert inv["availability"] == "disabled" and "containment" in inv["reason"]
    r = _investigate(client)
    assert r.status_code == 403 and "containment" in r.json()["reason"]


def test_offline_disables_and_refuses(client, services):
    client.post("/api/mode", json={"mode": "offline"}, headers=H("audit"))
    cat = client.get("/api/triggers/catalog", headers=H("em")).json()["manual"]
    assert all(t["availability"] == "disabled" and t["reason"] in ("offline", "role") for t in cat)
    assert _investigate(client).status_code == 403


def test_role_refusal(client):
    r = _investigate(client, role="pm")
    assert r.status_code == 403 and r.json()["error"] == "forbidden"
    cat = client.get("/api/triggers/catalog", headers=H("pm")).json()["manual"]
    assert next(t for t in cat if t["trigger_id"] == "investigate")["reason"] == "role"


def test_manual_run_is_admitted_audited_and_started_by_person(client, services):
    r = _investigate(client)
    assert r.status_code == 202
    run_id = r.json()["run_id"]
    assert r.json()["started_by"] == {"kind": "person", "role": "em"}
    run = client.get(f"/api/runs/{run_id}", headers=H("em")).json()
    assert run["started_by"]["kind"] == "person" and run["snapshot_id"] == "snap-v1.9.0"
    assert run["authoritative_generation"] == 1 and run["run_status"] == "complete"
    assert [e for e in services.audit.entries(run_id) if e["action"] == "trigger.investigate"]


def test_snapshot_binding_required(client):
    r = client.post("/api/triggers/manual/investigate", json={"event_id": "G04"}, headers=H("em"))
    assert r.status_code == 400 and r.json()["error"] == "snapshot_required"
    r = client.post("/api/triggers/manual/investigate", json={"snapshot_id": "nope", "event_id": "G04"}, headers=H("em"))
    assert r.status_code == 404


def test_idempotency_key_required_and_uuid(client):
    r = client.post("/api/triggers/manual/investigate", json={"snapshot_id": "snap-v1.9.0", "event_id": "G04"},
                    headers={"X-Atlas-Role": "em"})
    assert r.status_code == 400 and r.json()["error"] == "idempotency_key_required"
    r = client.post("/api/triggers/manual/investigate", json={"snapshot_id": "snap-v1.9.0", "event_id": "G04"},
                    headers={"X-Atlas-Role": "em", "Idempotency-Key": "not-a-uuid"})
    assert r.status_code == 400


def test_lost_response_retry_returns_stored_outcome_without_second_run(client, services):
    key = str(uuid.uuid4())
    first = _investigate(client, key=key)  # the client never saw this response
    again = _investigate(client, key=key)
    assert again.status_code == first.status_code == 202
    assert again.json()["run_id"] == first.json()["run_id"]
    assert again.headers.get("Idempotent-Replay") == "true"
    assert len(services.orchestrator.started) == 1


def test_same_key_different_payload_is_409_and_new_key_is_new_operation(client, services):
    key = str(uuid.uuid4())
    _investigate(client, key=key)
    assert _investigate(client, key=key, event="G05").status_code == 409
    other = _investigate(client)
    assert other.status_code == 202 and len(services.orchestrator.started) == 2


def test_uncertain_outcome_is_never_rerun(client, services):
    key = str(uuid.uuid4())
    services.idempotency._db.execute(
        "INSERT INTO idempotency VALUES (?,?,?,?,?,?,?)",
        (f"kubevirt::{key}", "kubevirt:em", "x", "pending", None, None, "2099-01-01T00:00:00+00:00"))
    r = _investigate(client, key=key)
    assert r.status_code == 409
    assert services.orchestrator.started == []


def test_reports_discovery_and_projection(client):
    run_id = _investigate(client).json()["run_id"]
    reports = client.get("/api/reports", params={"run_id": run_id}, headers=H("pm")).json()["reports"]
    assert {r["report_type"] for r in reports} == {"investigation", "security_gate"}
    pm = client.get(f"/api/reports/{run_id}:investigation", headers=H("oncall"))
    sec = client.get(f"/api/reports/{run_id}:investigation", headers=H("security"))
    assert "SECRET-FINDING-MARKER" not in pm.text and pm.json()["withheld"] >= 1
    assert "SECRET-FINDING-MARKER" in sec.text
    assert pm.json()["authoritative_generation"] == 1 and pm.json()["report_version"] == 1


def test_reverify_creates_new_generation_once_per_key_and_keeps_older_readable(client, services):
    run_id = _investigate(client).json()["run_id"]
    key = str(uuid.uuid4())
    r = client.post(f"/api/runs/{run_id}/reverify", json={}, headers=H("em", key))
    assert r.status_code == 200 and r.json()["authoritative_generation"] == 2
    replay = client.post(f"/api/runs/{run_id}/reverify", json={}, headers=H("em", key))
    assert replay.headers.get("Idempotent-Replay") == "true"
    assert services.orchestrator.runs.authoritative(run_id)[0] == 2  # no generation 3
    current = client.get(f"/api/reports/{run_id}:investigation", headers=H("oncall")).json()
    old = client.get(f"/api/reports/{run_id}:investigation", params={"generation": 1}, headers=H("oncall")).json()
    assert current["report_version"] == 2 and "public finding" not in current["markdown"]
    assert old["superseded_by"] == 2 and "public finding" in old["markdown"]
    assert client.post(f"/api/runs/{run_id}/reverify", json={}, headers=H("pm")).status_code == 403


def test_reverify_returns_task_outcomes_by_phase(client, services):
    services.orchestrator.action = "DELEGATE_TO_CODING_AGENT"
    run_id = _investigate(client).json()["run_id"]
    task = client.post("/api/tasks", json={"run_id": run_id}, headers=H("em")).json()["task"]
    r = client.post(f"/api/runs/{run_id}/reverify", json={}, headers=H("em")).json()
    assert r["tasks"][0]["state"] == "awaiting_reapproval"
    t = client.get(f"/api/tasks/{task['task_id']}", headers=H("em")).json()["task"]
    assert t["bound_generation"] == 2


def test_evidence_is_version_bound_and_projected(client):
    run_id = _investigate(client).json()["run_id"]
    ok = client.get("/api/evidence/rec-public", params={"run_id": run_id, "content_version_id": "cv1"}, headers=H("pm"))
    assert ok.status_code == 200 and ok.json()["record"]["url"].startswith("https://github.com/")
    wrong = client.get("/api/evidence/rec-public", params={"run_id": run_id, "content_version_id": "cv9"}, headers=H("pm"))
    assert wrong.status_code == 404 and wrong.json()["error"] == "not_in_run"


def test_graph_per_snapshot(client):
    snaps = client.get("/api/snapshots", params={"release": "1.9"}, headers=H("architect")).json()["snapshots"]
    assert {s["kind"] for s in snaps} >= {"prepared", "aliased"}
    a = client.get("/api/graph/snap-v1.9.0", headers=H("architect")).json()
    b = client.get("/api/graph/snap-v1.9.0-aliased", headers=H("architect")).json()
    assert a["nodes"] != b["nodes"]
    assert "SECRET-FINDING-MARKER" not in client.get("/api/graph/snap-v1.9.0", headers=H("pm")).text


def test_feedback_becomes_candidate(client, services, tmp_path):
    services.candidates_dir = tmp_path
    run_id = _investigate(client).json()["run_id"]
    r = client.post("/api/feedback", json={"run_id": run_id, "kind": "override", "item_id": "i1", "decision": "defer",
                                           "rationale": "Accepted for one release."}, headers=H("pm"))
    assert r.status_code == 201
    assert len(list(tmp_path.glob("*.json"))) == 1
    bad = client.post("/api/feedback", json={"run_id": run_id, "kind": "other", "rationale": "x"}, headers=H("pm"))
    assert bad.status_code == 400


def test_schedules_enable_pause_run_once(client, services):
    seen = []

    def poll(cursor):
        events = [ObservedEvent(key="issue-1:1", position="1", trigger={"trigger": "failure", "event_id": "issue-1"},
                                snapshot_id="snap-v1.9.0")]
        seen.append(cursor)
        return events

    services.scheduler.watchers["failure-watcher"] = Watcher("failure-watcher", "failure", poll)
    body = client.get("/api/schedules", headers=H("pm")).json()
    assert body["schedules"][0]["schedule_id"] == "failure-watcher"
    assert client.post("/api/schedules/failure-watcher/pause", json={}, headers=H("pm")).status_code == 403
    paused = client.post("/api/schedules/failure-watcher/pause", json={"reason": "maintenance"}, headers=H("em")).json()
    assert paused["schedule"]["state"] == "paused"
    once = client.post("/api/schedules/failure-watcher/run-once", json={}, headers=H("em")).json()
    assert len(once["admitted"]) == 1
    twice = client.post("/api/schedules/failure-watcher/run-once", json={}, headers=H("em")).json()
    assert twice["admitted"] == []  # the same event is a duplicate
    cat = client.get("/api/triggers/catalog", headers=H("em")).json()
    assert cat["automatic"][0]["schedule_id"] == "failure-watcher"


def test_runs_without_a_run_engine_fail_with_a_reason(services, client):
    services.orchestrator = None
    r = _investigate(client)
    run = client.get(f"/api/runs/{r.json()['run_id']}", headers=H("em")).json()
    assert run["run_status"] == "failed" and "not connected" in run["error"]


def test_evidence_ids_with_slashes_resolve(client, services):
    """Real record ids look like ``issues:issue/16936@abc``; the route must accept the slash."""
    record_id = "issues:issue/16936@6ce4e1c11e9fa2ea"
    services.snapshots.records[("snap-v1.9.0", record_id)] = {
        "record_id": record_id, "content_version_id": "cv1", "source": "issues",
        "access_scopes": ["public"], "provenance": "observed", "text": "flaky test"}
    run_id = _investigate(client).json()["run_id"]
    for path in (f"/api/evidence/{record_id}", "/api/evidence/issues%3Aissue%2F16936%406ce4e1c11e9fa2ea"):
        r = client.get(path, params={"run_id": run_id}, headers=H("pm"))
        assert r.status_code == 200, path
        assert r.json()["record"]["record_id"] == record_id
