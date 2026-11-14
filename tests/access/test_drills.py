"""Kill switch drills: a drill trips a switch through the real path, blocks what the switch blocks,
and ends on the audit role's confirmation."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from atlas.containment import (CONSUMERS, ROW_BY_SWITCH_ID, RULES, SWITCHES, ContainmentError, ContainmentStore)
from atlas.contracts import EgressPurpose
from atlas.execution.db import OpsDB
from tests.int.test_wiring_smoke import H, _delegated_run, _server

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)


def _drill(client, switch_id, key, role="audit", **body):
    return client.post(f"/api/containment/{switch_id}/drill", json=body, headers=H(role, key))


def _switch(client, switch_id, role="audit"):
    state = client.get("/api/containment", headers=H(role)).json()
    return next(s for s in state["switches"] if s["switch_id"] == switch_id)


def test_switch_list_is_readable(tmp_path):
    _, _, _, client = _server(tmp_path)
    switches = client.get("/api/containment", headers=H("audit")).json()["switches"]
    assert len(switches) == 11
    assert [s["switch_id"] for s in switches][:2] == ["write-outside-sandbox", "change-after-approval"]
    for s in switches:
        assert not s["switch_id"].isdigit() and s["title"][0].isupper() and s["title"] == s["title"].capitalize()
        assert s["state"] == "ok" and s["can_drill"] is True and s["incident"] is None
        assert s["last_tripped_at"] is None and s["blocks"] and all(" " in b or b[0].isupper() for b in s["blocks"])


def test_only_audit_can_start_a_drill(tmp_path):
    _, _, _, client = _server(tmp_path)
    for role in ("pm", "em", "security", "oncall", "architect"):
        assert _drill(client, "quality-below-floor", f"k-{role}", role=role).status_code == 403
    assert _drill(client, "no-such-switch", "k-x").status_code == 404
    assert client.get("/api/containment", headers=H("audit")).json()["incidents"] == []


def test_drill_trips_blocks_shows_in_catalog_and_resumes(tmp_path):
    services, wiring, _, client = _server(tmp_path)
    r = _drill(client, "outside-connection-while-offline", "k-d1", note="monthly check")
    assert r.status_code == 201, r.text
    inc = r.json()["incident"]
    assert inc["drill"] is True and inc["kind"] == "Drill" and inc["open"] is True
    assert inc["triggered_by"]["role"] == "audit" and inc["note"] == "monthly check"
    assert inc["switch_id"] == "outside-connection-while-offline"
    assert inc["required_prerequisites"] == ["drill_acknowledged"]

    sw = _switch(client, "outside-connection-while-offline")
    assert sw["state"] == "drill" and sw["can_drill"] is False and sw["incident"]["drill"] is True
    assert sw["last_tripped_at"] is not None

    # consumers are really blocked: model calls, publication, the trigger catalog and admission
    assert not wiring.containment.permits("model_calls").allowed
    assert not wiring.containment.permits("publication").allowed
    assert wiring.containment.permits("preparation").allowed
    cat = client.get("/api/triggers/catalog", headers=H("em")).json()["manual"]
    reason = "kill switch Outside connection attempted while offline (drill)"
    assert all(t["availability"] == "disabled" and t["reason"] == reason for t in cat)
    refused = client.post("/api/triggers/question", json={"snapshot_id": "s", "question": "q"}, headers=H("pm", "k-q"))
    assert refused.status_code == 403 and reason in refused.json()["reason"]
    with pytest.raises(Exception):
        wiring.outbound.call("investigator", EgressPurpose.MODEL_CALL, {"messages": [{"role": "user", "content": "x"}]})

    # the notification is marked as a drill
    notes = client.get("/api/notifications", headers=H("audit")).json()["notifications"]
    drill_note = next(n for n in notes if n["event_type"] == "containment.tripped")
    assert drill_note["message"].startswith("Drill:") and drill_note["content"]["drill"] is True

    # a second drill on the same switch is refused; another role cannot end it
    assert _drill(client, "outside-connection-while-offline", "k-d2").status_code == 409
    assert client.post(f"/api/incidents/{inc['incident_id']}/resume", json={}, headers=H("em", "k-r0")).status_code == 403

    done = client.post(f"/api/incidents/{inc['incident_id']}/resume", json={}, headers=H("audit", "k-r1"))
    assert done.status_code == 200 and done.json()["incident"]["open"] is False
    assert [p["kind"] for p in done.json()["incident"]["recorded_prerequisites"]] == ["drill_acknowledged"]
    assert wiring.containment.permits("model_calls").allowed
    assert _switch(client, "outside-connection-while-offline")["state"] == "ok"
    cat = client.get("/api/triggers/catalog", headers=H("em")).json()["manual"]
    assert all(t["availability"] == "available" for t in cat)
    audited = {e["action"] for e in services.audit.entries()}
    assert {"containment.drill", "incident.resume"} <= audited


def test_drill_blocks_task_preparation_and_approval(tmp_path):
    services, wiring, _, client = _server(tmp_path)
    run_id = _delegated_run(services, wiring, "snap")
    task = client.post("/api/tasks", json={"run_id": run_id}, headers=H("em", "k-task")).json()["task"]
    inc = _drill(client, "sandbox-rule-broken", "k-d").json()["incident"]
    assert not wiring.containment.permits("preparation").allowed
    assert not wiring.containment.permits("execution").allowed
    assert wiring.containment.permits("model_calls").allowed
    prep = client.post(f"/api/tasks/{task['task_id']}/prepare", json={}, headers=H("em", "k-prep"))
    assert prep.status_code in (403, 409) and "drill" in prep.text
    client.post(f"/api/incidents/{inc['incident_id']}/resume", json={}, headers=H("audit", "k-r"))
    assert wiring.containment.permits("preparation").allowed


@pytest.mark.parametrize("consumer", CONSUMERS)
def test_a_drill_blocks_each_kind_of_work(consumer):
    store = ContainmentStore(OpsDB())
    row = next(r for r, rule in sorted(RULES.items()) if consumer in rule.blocks)
    incident = store.trip(row, producer="drill", detail={}, now=NOW, drill=True)
    verdict = store.permits(consumer)
    assert not verdict.allowed and verdict.reasons == (f"kill switch {SWITCHES[row].title} (drill)",)
    for other in CONSUMERS:
        assert store.permits(other).allowed is (other not in RULES[row].blocks)
    store.resume(incident, [], role="audit", now=NOW)
    assert all(store.permits(c).allowed for c in CONSUMERS)


def test_every_switch_can_be_drilled_by_its_id():
    store = ContainmentStore(OpsDB())
    assert sorted(ROW_BY_SWITCH_ID.values()) == sorted(RULES)
    for row in RULES:
        inc = store.trip(row, producer="drill", detail={}, now=NOW, drill=True)
        assert store.incident(inc)["drill"] is True
        store.resume(inc, [], role="audit", now=NOW)
    assert store.open_incidents() == []


def test_drill_does_not_void_approvals_but_a_real_trip_does():
    db = OpsDB()
    voided: list[str] = []
    store = ContainmentStore(db, on_void_approvals=lambda conn, incident, now: voided.append(incident))
    drill = store.trip(7, producer="drill", detail={}, now=NOW, drill=True)
    assert voided == []
    real = store.trip(7, producer="outbound", detail={"run_id": "run-1"}, now=NOW)
    assert real == drill and voided == [drill]


def test_real_trip_during_a_drill_makes_it_a_real_incident():
    store = ContainmentStore(OpsDB())
    drill = store.trip(4, producer="drill", detail={"note": "check"}, now=NOW, drill=True)
    real = store.trip(4, producer="outbound", detail={"host": "example.invalid"}, now=NOW)
    assert real == drill and len(store.open_incidents()) == 1
    inc = store.incident(real)
    assert inc["drill"] is False and inc["required_prerequisites"] == ["devops_audit", "owner_signoff"]
    with pytest.raises(ContainmentError):
        store.resume(real, [], role="audit", now=NOW)  # the audit confirmation alone is no longer enough
    a = store.record_prerequisite(real, "devops_audit", produced_by="api", detail={}, now=NOW)
    b = store.record_prerequisite(real, "owner_signoff", produced_by="api", detail={}, now=NOW)
    store.resume(real, [a, b], role="audit", now=NOW)
    assert store.permits("model_calls").allowed


def test_no_drill_while_a_real_incident_is_open():
    store = ContainmentStore(OpsDB())
    store.trip(4, producer="outbound", detail={}, now=NOW)
    with pytest.raises(ContainmentError, match="real incident"):
        store.trip(4, producer="drill", detail={}, now=NOW, drill=True)
    store.trip(5, producer="drill", detail={}, now=NOW, drill=True)  # another switch is fine


def test_drill_acknowledgement_cannot_be_recorded_by_hand_or_used_for_a_real_incident():
    store = ContainmentStore(OpsDB())
    real = store.trip(4, producer="outbound", detail={}, now=NOW)
    with pytest.raises(ContainmentError):
        store.record_prerequisite(real, "drill_acknowledged", produced_by="api", detail={}, now=NOW)


def test_only_audit_ends_a_drill():
    store = ContainmentStore(OpsDB())
    inc = store.trip(11, producer="drill", detail={}, now=NOW, drill=True)
    with pytest.raises(ContainmentError):
        store.resume(inc, [], role="em", now=NOW)


def test_an_open_drill_survives_a_restart(tmp_path):
    path = tmp_path / "ops.sqlite"
    first = ContainmentStore(OpsDB(path))
    inc = first.trip(9, producer="drill", detail={"note": "before restart"}, now=NOW, drill=True)
    second = ContainmentStore(OpsDB(path))  # a new process opening the same file
    assert [i["incident_id"] for i in second.open_incidents()] == [inc]
    assert second.incident(inc)["drill"] is True and not second.permits("report_delivery").allowed
    second.resume(inc, [], role="audit", now=NOW)
    assert ContainmentStore(OpsDB(path)).open_incidents() == []
