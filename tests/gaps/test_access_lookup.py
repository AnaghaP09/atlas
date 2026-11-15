"""Every kind of item in the access table can be found, read or requested through the real wiring."""

from __future__ import annotations

from datetime import datetime, timezone

from atlas.access import KINDS
from atlas.contracts import StartedBy, StartedByKind
from atlas.scheduler import manual_admission

from tests.gaps.conftest import headers as h


def _ask(client, role, object_type, object_id):
    return client.post("/api/access-requests", headers=h(role),
                       json={"object_type": object_type, "object_id": object_id})


def test_kill_switch_is_found_by_word_id_and_can_be_requested(server):
    services, client, _ = server
    # Not tripped: the switch still exists as an item.
    locked = client.get("/api/objects/kill_switch/write-outside-sandbox", headers=h("pm"))
    assert locked.status_code == 403 and locked.json()["error"] == "locked"
    assert locked.json()["needs_decision"] is True and locked.json()["owner_role"] == "audit"
    asked = _ask(client, "pm", "kill_switch", "write-outside-sandbox")
    assert asked.status_code == 201 and asked.json()["request"]["status"] == "pending"
    rid = asked.json()["request"]["request_id"]
    assert client.post(f"/api/access-requests/{rid}/decision", headers=h("audit"),
                       json={"decision": "grant", "hours": 1}).status_code == 200
    opened = client.get("/api/objects/kill_switch/write-outside-sandbox", headers=h("pm"))
    assert opened.status_code == 200 and opened.json()["read_only"] is True
    assert opened.json()["switch"]["switch_id"] == "write-outside-sandbox"
    # The row number names the same switch; an unknown id is not found.
    assert client.get("/api/objects/kill_switch/1", headers=h("audit")).json()["switch"]["switch_id"] == "write-outside-sandbox"
    assert client.get("/api/objects/kill_switch/no-such-switch", headers=h("audit")).status_code == 404
    assert _ask(client, "pm", "kill_switch", "no-such-switch").status_code == 404


def test_incident_is_found_while_open_and_after_it_is_resumed(server):
    services, client, _ = server
    drill = client.post("/api/containment/sandbox-rule-broken/drill", headers=h("audit"), json={"note": "check"})
    assert drill.status_code == 201
    incident_id = drill.json()["incident"]["incident_id"]
    assert client.get(f"/api/objects/incident/{incident_id}", headers=h("audit")).status_code == 200
    assert client.get(f"/api/objects/incident/{incident_id}", headers=h("pm")).status_code == 403
    related = client.get("/api/related/kill_switch/sandbox-rule-broken", headers=h("audit")).json()["related"]
    assert {"incident"} == {r["object_type"] for r in related}
    back = client.get(f"/api/related/incident/{incident_id}", headers=h("audit")).json()["related"]
    assert [(r["object_type"], r["object_id"]) for r in back] == [("kill_switch", "sandbox-rule-broken")]
    assert client.post(f"/api/incidents/{incident_id}/resume", headers=h("audit"), json={}).status_code == 200
    after = client.get(f"/api/objects/incident/{incident_id}", headers=h("audit"))
    assert after.status_code == 200 and after.json()["incident"]["open"] is False
    assert client.get("/api/objects/incident/inc-missing", headers=h("audit")).status_code == 404


def test_scorecard_row_is_found_and_links_to_its_task(server):
    services, client, _ = server
    w = services.wiring
    w.scorecard.write_row("task-1", "test_defect", {"accepted": True}, datetime.now(timezone.utc))
    rows = client.get("/api/scorecard", headers=h("em")).json()["rows"]
    row_id = str(rows[0].get("row_id") or rows[0]["task_id"])
    assert client.get(f"/api/objects/scorecard_row/{row_id}", headers=h("em")).status_code == 200
    assert client.get(f"/api/objects/scorecard_row/{row_id}", headers=h("pm")).status_code == 403
    granted = _ask(client, "pm", "scorecard_row", row_id)  # an ordinary item: granted at once
    assert granted.status_code == 201 and granted.json()["request"]["status"] == "granted"
    assert client.get(f"/api/objects/scorecard_row/{row_id}", headers=h("pm")).json()["read_only"] is True
    assert client.get("/api/objects/scorecard_row/none", headers=h("em")).status_code == 404


def test_evidence_is_read_inside_the_run_that_used_it(server, monkeypatch):
    services, client, _ = server
    snap = client.post("/api/snapshots", headers=h("em"), json={}).json()
    adm = manual_admission(services.admissions, "k1", {"trigger": "question", "question": "q"}, snap["snapshot_id"],
                           StartedBy(kind=StartedByKind.PERSON, role="pm"), project_id="alpha")
    record = {"record_id": "issues:issue/7@v1", "source": "issues", "access_scopes": ["public"], "content": {"n": 7}}
    monkeypatch.setattr(services.snapshots, "evidence",
                        lambda sid, rid, version, lineage=(): dict(record) if rid == record["record_id"] else None)
    found = client.get(f"/api/objects/evidence/{adm.run_id}::{record['record_id']}", headers=h("architect"))
    assert found.status_code == 200 and found.json()["record"]["record_id"] == record["record_id"]
    related = client.get(f"/api/related/evidence/{adm.run_id}::{record['record_id']}", headers=h("architect")).json()
    assert [(r["object_type"], r["object_id"]) for r in related["related"]] == [("run", adm.run_id)]
    assert client.get(f"/api/objects/evidence/{adm.run_id}::issues:issue/8@v1", headers=h("architect")).status_code == 404
    assert client.get("/api/objects/evidence/issues:issue/7@v1", headers=h("architect")).status_code == 404


def test_every_kind_in_the_table_has_a_lookup(server):
    """A made-up id of each kind answers 404 (not found), never a server error or a blanket refusal."""
    services, client, _ = server
    for kind in sorted({k.split(":")[0] for k in KINDS}):
        if kind == "dead_letter":
            assert client.get("/api/objects/dead_letter/list", headers=h("audit")).status_code == 200
            continue
        out = client.get(f"/api/objects/{kind}/made-up-id", headers=h("audit"))
        assert out.status_code == 404, (kind, out.status_code, out.text)
        assert client.get(f"/api/related/{kind}/made-up-id", headers=h("audit")).status_code == 404
