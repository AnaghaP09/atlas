"""Streams, notifications, tasks, mode, containment and corrections."""

from __future__ import annotations

import json
import uuid

from atlas.api.streams import StreamHub, encode, to_wire
from atlas.contracts import EVENT_NAMES
from atlas.projection import AuthorizationProjection, principal_for
from atlas.reports import EventProducer
from tests.d.conftest import H


def _frames(text: str) -> list[dict]:
    out = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line and not line.startswith(":"))
        if "data" in lines:
            out.append({"id": lines.get("id"), "event": lines["event"], "data": json.loads(lines["data"])})
    return out


SCHEMA_FIELDS = {"event", "event_id", "emitted_at", "run_id", "authoritative_generation", "report_version",
                 "correction_version", "data"}


def test_only_two_stream_paths_exist(app):
    streams = [p for p in app.openapi()["paths"] if p.endswith("/stream")]
    assert sorted(streams) == ["/api/notifications/stream", "/api/runs/{run_id}/stream"]


def test_run_stream_carries_schema_events_from_the_real_producer(client):
    run_id = client.post("/api/triggers/failure", json={"snapshot_id": "snap-v1.9.0", "event_id": "G04"},
                         headers=H("em")).json()["run_id"]
    client.post(f"/api/runs/{run_id}/reverify", json={}, headers=H("em"))
    r = client.get(f"/api/runs/{run_id}/stream", params={"follow": "false"}, headers={"X-Atlas-Role": "pm"})
    assert r.headers["content-type"].startswith("text/event-stream")
    frames = _frames(r.text)
    names = [f["event"] for f in frames]
    assert names == ["run.status_changed", "run.generation_changed", "report.superseded"]
    for f in frames:
        assert set(f["data"]) == SCHEMA_FIELDS and f["event"] in EVENT_NAMES and f["id"] == f["data"]["event_id"]
    assert frames[-1]["data"]["authoritative_generation"] == 2 and frames[-1]["data"]["report_version"] == 2


def test_last_event_id_replay_and_reset_on_gap(client):
    run_id = client.post("/api/triggers/failure", json={"snapshot_id": "snap-v1.9.0", "event_id": "G04"},
                         headers=H("em")).json()["run_id"]
    client.post(f"/api/runs/{run_id}/reverify", json={}, headers=H("em"))
    first = _frames(client.get(f"/api/runs/{run_id}/stream", params={"follow": "false"}, headers=H("pm")).text)
    later = _frames(client.get(f"/api/runs/{run_id}/stream", params={"follow": "false"},
                               headers={"X-Atlas-Role": "pm", "Last-Event-ID": first[0]["id"]}).text)
    assert [f["id"] for f in later] == [f["id"] for f in first[1:]]
    gap = _frames(client.get(f"/api/runs/{run_id}/stream", params={"follow": "false"},
                             headers={"X-Atlas-Role": "pm", "Last-Event-ID": "evt-long-gone"}).text)
    assert gap[0]["event"] == "stream.reset" and gap[0]["id"] is None
    assert set(gap[0]["data"]["data"]) == {"reason", "resume_from"} and gap[0]["data"]["data"]["reason"] == "buffer_gap"
    assert gap[1]["id"] == gap[0]["data"]["data"]["resume_from"]


def test_buffer_gap_through_the_hub():
    proj = AuthorizationProjection()
    hub = StreamHub(proj, buffer_size=3)
    prod = EventProducer(proj)
    prod.subscribers.append(hub.on_run_event)
    ids = [prod.emit("run.progress", run_id="r", authoritative_generation=1, required_scopes=["public"])["event_id"]
           for _ in range(5)]
    reset, events = hub.read("runs", principal_for("pm"), ids[0], "r")
    assert reset["data"]["resume_from"] == ids[2]
    assert [e["event_id"] for e in events] == ids[2:]


def test_stream_projection_never_delivers_restricted_events_to_pm():
    proj = AuthorizationProjection()
    hub = StreamHub(proj)
    prod = EventProducer(proj)
    prod.subscribers.append(hub.on_run_event)
    prod.emit("run.progress", run_id="r", authoritative_generation=1, payload={"finding": "SECRET-FINDING-MARKER"},
              required_scopes=["restricted:security"])
    assert hub.read("runs", principal_for("pm"))[1] == []
    assert hub.read("runs", principal_for("security"))[1][0]["data"]["finding"] == "SECRET-FINDING-MARKER"


def test_wire_shape_and_encoding():
    wire = to_wire({"event": "run.progress", "event_id": "e1", "emitted_at": "t", "run_id": "r",
                    "authoritative_generation": 1, "report_version": 1, "correction_version": 0,
                    "payload": {"step": 1}, "agent": "scout"})
    assert wire["data"] == {"step": 1, "agent": "scout"}
    assert encode(wire).startswith("id: e1\nevent: run.progress\ndata: ")


def test_notifications_list_stream_ack_and_roles(client, services):
    services.notifier.emit("connector.failed", "gateway", ["gateway"], frozenset({"internal"}),
                           {"text": "connector failed after its retry", "required_scopes": ["internal"]})
    oncall = client.get("/api/notifications", headers=H("oncall")).json()["notifications"]
    assert len(oncall) == 1 and oncall[0]["severity"] == "warning"
    assert client.get("/api/notifications", headers=H("pm")).json()["notifications"] == []
    nid = oncall[0]["id"]
    frames = _frames(client.get("/api/notifications/stream", params={"follow": "false"}, headers=H("oncall")).text)
    assert frames[0]["event"] == "notification.created" and frames[0]["data"]["data"]["notification_id"] == nid
    assert _frames(client.get("/api/notifications/stream", params={"follow": "false"}, headers=H("pm")).text) == []
    assert client.post(f"/api/notifications/{nid}/ack", headers=H("pm")).status_code == 403
    assert client.post(f"/api/notifications/{nid}/ack", headers=H("oncall")).status_code == 200
    assert client.post(f"/api/notifications/{nid}/resolve", headers=H("audit")).status_code == 200
    states = client.get("/api/notifications", headers=H("oncall")).json()["notifications"]
    assert states[0]["status"] == "resolved"
    assert client.get("/api/notifications/dead-letter", headers=H("pm")).status_code == 403
    assert client.get("/api/notifications/dead-letter", headers=H("audit")).status_code == 200


def test_destinations_are_staged(client):
    body = client.get("/api/destinations", headers=H("audit")).json()
    assert body["status"] == "not_available_in_demo" and body["data_mode"] == "STAGED"


def test_mode_change_is_audit_only(client):
    assert client.post("/api/mode", json={"mode": "offline"}, headers=H("em")).status_code == 403
    assert client.post("/api/mode", json={"mode": "offline"}, headers=H("audit")).json()["mode"] == "offline"
    assert client.get("/api/mode", headers=H("pm")).json()["mode"] == "offline"


def _delegating_run(client, services):
    services.orchestrator.action = "DELEGATE_TO_CODING_AGENT"
    return client.post("/api/triggers/failure", json={"snapshot_id": "snap-v1.9.0", "event_id": "G01"},
                       headers=H("oncall")).json()["run_id"]


def test_task_lifecycle_through_the_api(client, services):
    run_id = _delegating_run(client, services)
    created = client.post("/api/tasks", json={"run_id": run_id}, headers=H("em"))
    assert created.status_code == 201
    task = created.json()["task"]
    assert task["state"] == "proposed" and task["security"]["state"] == "passed"
    assert all(task[f] is None for f in ("ci_observations", "behavioural_check", "adjudication", "pull_request"))
    approval = client.get("/api/approvals", headers=H("em")).json()["approvals"][0]
    assert client.post(f"/api/approvals/{approval['approval_id']}/decision", json={"decision": "approve"},
                       headers=H("pm")).status_code == 403
    d = client.post(f"/api/approvals/{approval['approval_id']}/decision",
                    json={"decision": "approve", "review_minutes": 12}, headers=H("em"))
    assert d.status_code == 200 and d.json()["approval"]["review_minutes"] == 12
    tid = task["task_id"]
    assert client.post(f"/api/tasks/{tid}/prepare", headers=H("em")).json()["task"]["state"] == "prepared"
    pub = client.post(f"/api/tasks/{tid}/publish", headers=H("em")).json()["task"]
    assert pub["state"] == "published" and pub["pull_request"]
    bad = client.post(f"/api/tasks/{tid}/adjudicate", json={"verdict": "maybe"}, headers=H("em"))
    assert bad.status_code == 400
    ok = client.post(f"/api/tasks/{tid}/adjudicate", json={"verdict": "correct", "note": "fix verified"}, headers=H("em"))
    assert ok.json()["task"]["state"] == "accepted"


def test_task_creation_needs_a_delegation_and_connected_mode(client, services):
    run_id = client.post("/api/triggers/failure", json={"snapshot_id": "snap-v1.9.0", "event_id": "G02"},
                         headers=H("em")).json()["run_id"]
    assert client.post("/api/tasks", json={"run_id": run_id}, headers=H("em")).status_code == 409
    run2 = _delegating_run(client, services)
    client.post("/api/mode", json={"mode": "offline"}, headers=H("audit"))
    assert client.post("/api/tasks", json={"run_id": run2}, headers=H("em")).status_code == 403


def test_refusals_from_execution_layer_pass_through(client, services):
    run_id = _delegating_run(client, services)
    tid = client.post("/api/tasks", json={"run_id": run_id}, headers=H("em")).json()["task"]["task_id"]
    services.tasks.refuse_next = "security_expired"
    r = client.post(f"/api/tasks/{tid}/prepare", headers=H("em"))
    assert r.status_code == 403 and r.json()["reason"] == "security_expired"


def test_security_refresh_in_proposed_keeps_proposed(client, services):
    run_id = _delegating_run(client, services)
    tid = client.post("/api/tasks", json={"run_id": run_id}, headers=H("em")).json()["task"]["task_id"]
    r = client.post(f"/api/tasks/{tid}/security-refresh", headers=H("em"))
    assert r.json()["task"]["state"] == "proposed"


def test_unknown_task_is_404_before_any_effect(client, services):
    r = client.post("/api/tasks/task-nope/prepare", headers=H("em"))
    assert r.status_code == 404 and services.tasks.calls == []


def test_containment_prerequisites_resume_and_correction_kinds(client, services):
    incident = services.containment.trip("publication", "unsupported claim published")
    sw = client.get("/api/containment/publication", headers=H("audit")).json()
    assert sw["switch"]["incidents"][0]["incident_id"] == incident
    assert client.post(f"/api/incidents/{incident}/prerequisites", json={"kind": "correction_published"},
                       headers=H("audit")).status_code == 400
    other = client.post(f"/api/incidents/{incident}/prerequisites",
                        json={"kind": "owner_signoff", "reference": "other-incident-signoff"}, headers=H("audit"))
    assert other.status_code == 409
    pre = client.post(f"/api/incidents/{incident}/prerequisites", json={"kind": "owner_signoff", "reference": "s1"},
                      headers=H("audit")).json()["prerequisite"]
    assert client.post(f"/api/incidents/{incident}/resume", json={"prerequisite_ids": []},
                       headers=H("audit")).status_code == 409
    assert client.post(f"/api/incidents/{incident}/resume", json={"prerequisite_ids": [pre["prerequisite_id"]]},
                       headers=H("audit")).status_code == 200


def test_corrections_held_outcome_is_rediscoverable_without_another_write(client, services):
    incident = services.containment.trip("publication", "unsupported claim")
    auth = client.post("/api/corrections/authorisations", json={"incident_id": incident, "target_type": "pr_description"},
                       headers=H("audit")).json()["authorisation"]
    body = {"incident_id": incident, "authorisation_id": auth["authorisation_id"], "target_type": "pr_description",
            "target_id": "pr-1", "item_id": "claim-3", "expected_body_hash": "h1"}
    services.containment.next_outcome = "held"
    key = str(uuid.uuid4())
    r = client.post("/api/corrections", json=body, headers=H("audit", key))
    assert r.status_code == 202 and r.json()["status"] == "held"
    # the client reloads and lists corrections instead of writing again
    listed = client.get("/api/corrections", params={"incident_id": incident}, headers=H("audit")).json()
    assert listed["corrections"][0]["state"] == "held"
    again = client.post("/api/corrections", json=body, headers=H("audit", key))
    assert again.headers.get("Idempotent-Replay") == "true"
    assert services.containment.writes == 1


def test_stale_correction_is_refused(client, services):
    incident = services.containment.trip("publication", "x")
    body = {"incident_id": incident, "authorisation_id": "a", "target_type": "report_notice", "target_id": "r",
            "item_id": "i", "expected_body_hash": "stale"}
    r = client.post("/api/corrections", json=body, headers=H("audit"))
    assert r.status_code == 409 and "stale_body" in r.json()["reason"]
    assert client.post("/api/corrections", json=dict(body, target_type="comment"), headers=H("audit")).status_code == 400
