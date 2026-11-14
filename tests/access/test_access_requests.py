"""Locked items, automatic grants, decisions by the owning role, expiry and revocation."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from atlas.access import KINDS, MAX_PENDING_PER_USER, AccessError, AccessRequestStore, policy_for
from atlas.api.routes_access import sweep_expiring
from tests.d.conftest import H

SENSITIVE = {"report:security_gate", "finding", "incident", "kill_switch", "dead_letter"}


def _seed(client, services):
    """A delegated run with its reports, one task and its approval."""
    services.orchestrator.action = "DELEGATE_TO_CODING_AGENT"
    run_id = client.post("/api/triggers/manual/investigate", json={"snapshot_id": "snap-v1.9.0", "event_id": "G01"},
                         headers=H("em")).json()["run_id"]
    task = client.post("/api/tasks", json={"run_id": run_id}, headers=H("em")).json()["task"]
    approval_id = next(iter(services.tasks.approval_rows))
    return run_id, task["task_id"], approval_id


def _ask(client, role, object_type, object_id, **extra):
    return client.post("/api/access-requests", json={"object_type": object_type, "object_id": object_id, **extra},
                       headers=H(role))


def _decide(client, role, request_id, **body):
    return client.post(f"/api/access-requests/{request_id}/decision", json=body, headers=H(role))


# ------------------------------------------------------------------ the table

@pytest.mark.parametrize("kind", sorted(KINDS))
def test_every_kind_has_an_owner_and_the_right_sensitivity(kind):
    object_type, _, subtype = kind.partition(":")
    p = policy_for(object_type, subtype or None, ["internal"])
    assert p is not None and p.owner in {"pm", "em", "architect", "security", "audit"}
    assert p.sensitive is (kind in SENSITIVE)
    assert "audit" in p.readers and p.grantable


def test_restricted_security_scope_makes_any_item_sensitive_and_security_owned():
    p = policy_for("task", None, ["internal", "restricted:security"])
    assert p.sensitive and p.owner == "security" and p.readers <= {"security", "audit"}


def test_unknown_scopes_are_never_grantable():
    p = policy_for("task", None, None)
    assert not p.grantable and p.readers == frozenset()


def test_unknown_kind_has_no_policy():
    assert policy_for("spaceship", None, ["internal"]) is None


# ------------------------------------------------------------------ ordinary items: granted at once

def test_ordinary_item_is_locked_then_granted_at_once_read_only(client, services):
    _, task_id, approval_id = _seed(client, services)
    locked = client.get(f"/api/approvals/{approval_id}", headers=H("pm"))
    assert locked.status_code == 403
    body = locked.json()
    assert body["error"] == "locked" and body["requestable"] and body["owner_role"] == "em"
    assert body["needs_decision"] is False and body["request"] is None
    assert task_id not in locked.text  # nothing of the content

    r = _ask(client, "pm", "approval", approval_id, reason="planning")
    assert r.status_code == 201
    req = r.json()["request"]
    assert r.json()["access"] == "open" and req["status"] == "granted"
    assert req["grant"]["automatic"] and req["grant"]["read_only"] and req["grant"]["active"]
    lifetime = datetime.fromisoformat(req["grant"]["expires_at"]) - datetime.fromisoformat(req["grant"]["created_at"])
    assert lifetime == timedelta(hours=24)

    ok = client.get(f"/api/approvals/{approval_id}", headers=H("pm"))
    assert ok.status_code == 200 and ok.json()["read_only"] is True and ok.json()["approval"]["task_id"] == task_id
    # exactly that one item: the task stays locked
    assert client.get(f"/api/tasks/{task_id}", headers=H("pm")).status_code == 403


def test_a_grant_never_allows_a_change(client, services):
    _, task_id, approval_id = _seed(client, services)
    assert _ask(client, "pm", "approval", approval_id).json()["access"] == "open"
    assert _ask(client, "pm", "task", task_id).json()["access"] == "open"
    before = list(services.tasks.calls)
    assert client.post(f"/api/approvals/{approval_id}/decision", json={"decision": "approve"},
                       headers=H("pm")).status_code == 403
    for step in ("prepare", "publish", "security-refresh"):
        assert client.post(f"/api/tasks/{task_id}/{step}", json={}, headers=H("pm")).status_code == 403
    assert client.post(f"/api/tasks/{task_id}/adjudicate", json={"verdict": "correct"}, headers=H("pm")).status_code == 403
    assert client.post("/api/tasks", json={"run_id": "x"}, headers=H("pm")).status_code == 403
    assert services.tasks.calls == before


def test_owner_is_told_and_can_revoke_and_the_item_locks_again(client, services):
    _, _, approval_id = _seed(client, services)
    req = _ask(client, "pm", "approval", approval_id).json()["request"]
    notes = client.get("/api/notifications", headers=H("em")).json()["notifications"]
    assert any(n["event_type"] == "access.granted_automatically" and n["subject_id"] == req["request_id"] for n in notes)
    box = client.get("/api/access-requests", params={"box": "to_decide"}, headers=H("em")).json()["requests"]
    assert [b["request_id"] for b in box] == [req["request_id"]]

    grant_id = req["grant"]["grant_id"]
    assert client.post(f"/api/access-grants/{grant_id}/revoke", json={}, headers=H("pm")).status_code == 403
    revoked = client.post(f"/api/access-grants/{grant_id}/revoke", json={}, headers=H("em"))
    assert revoked.status_code == 200 and revoked.json()["request"]["status"] == "revoked"
    again = client.get(f"/api/approvals/{approval_id}", headers=H("pm"))
    assert again.status_code == 403 and again.json()["request"]["status"] == "revoked"
    # the open stream tells the requester, and never carried the item itself
    stream = client.get("/api/notifications/stream", params={"follow": "false"}, headers=H("pm")).text
    assert "access.revoked" in stream and approval_id not in stream
    # asking again after a revoke makes a new request
    assert _ask(client, "pm", "approval", approval_id).json()["request"]["request_id"] != req["request_id"]


def test_restricted_content_inside_a_granted_item_stays_withheld(client, services):
    run_id, _, _ = _seed(client, services)
    report_id = f"{run_id}:investigation"
    assert client.get(f"/api/reports/{report_id}", headers=H("pm")).status_code == 403
    assert _ask(client, "pm", "report", report_id).json()["access"] == "open"
    got = client.get(f"/api/reports/{report_id}", headers=H("pm"))
    assert got.status_code == 200 and got.json()["read_only"] is True
    assert "public finding" in got.text and "SECRET-FINDING-MARKER" not in got.text
    related = client.get(f"/api/related/report/{report_id}", headers=H("pm")).json()["related"]
    finding = next(r for r in related if r["object_type"] == "finding")
    assert finding["access"] == "locked" and finding["requestable"] and finding["needs_decision"]
    assert finding["label"] == "Security finding" and "SECRET" not in str(finding)


# ------------------------------------------------------------------ sensitive items: a person decides

def test_sensitive_item_waits_for_the_owning_role(client, services):
    run_id, _, _ = _seed(client, services)
    finding_id = f"{run_id}:1"
    assert client.get(f"/api/objects/finding/{finding_id}", headers=H("pm")).status_code == 403
    r = _ask(client, "pm", "finding", finding_id)
    req = r.json()["request"]
    assert r.status_code == 201 and r.json()["access"] == "locked" and req["status"] == "pending"
    assert req["owner_role"] == "security" and req["needs_decision"] and req["grant"] is None
    assert client.get(f"/api/objects/finding/{finding_id}", headers=H("pm")).json()["request"]["status"] == "pending"
    # the owning role is told and gets a "needs a person" entry
    notes = client.get("/api/notifications", headers=H("security")).json()["notifications"]
    assert any(n["event_type"] == "access.requested" for n in notes)
    assert not any(n["event_type"] == "access.requested" for n in
                   client.get("/api/notifications", headers=H("em")).json()["notifications"])
    assert any(e["trigger"] == "access_request" and e["subject_id"] == req["request_id"]
               for e in client.get("/api/queue", headers=H("security")).json()["entries"])

    rid = req["request_id"]
    em = _decide(client, "em", rid, decision="grant")
    assert em.status_code == 403 and "security" in em.json()["reason"]
    own = _decide(client, "pm", rid, decision="grant")
    assert own.status_code == 403 and "own request" in own.json()["reason"]
    assert _decide(client, "security", rid, decision="grant", hours=500).status_code == 400
    ok = _decide(client, "security", rid, decision="grant", hours=1)
    assert ok.status_code == 200 and ok.json()["request"]["grant"]["automatic"] is False
    assert ok.json()["request"]["grant"]["granted_by"]["role"] == "security"

    read = client.get(f"/api/objects/finding/{finding_id}", headers=H("pm"))
    assert read.status_code == 200 and "SECRET-FINDING-MARKER" in read.text and read.json()["via_grant"] is True
    assert any(n["event_type"] == "access.decided" for n in
               client.get("/api/notifications", headers=H("pm")).json()["notifications"])
    # one finding only: the report's restricted part is still withheld from a pm reader
    assert _ask(client, "pm", "report", f"{run_id}:investigation").json()["access"] == "open"
    assert "SECRET-FINDING-MARKER" not in client.get(f"/api/reports/{run_id}:investigation", headers=H("pm")).text

    # expiry hides it again
    later = datetime.now(timezone.utc) + timedelta(hours=2)
    services.access.clock = lambda: later
    gone = client.get(f"/api/objects/finding/{finding_id}", headers=H("pm"))
    assert gone.status_code == 403 and gone.json()["request"]["status"] == "expired"


def test_deny_and_withdraw(client, services):
    run_id, _, _ = _seed(client, services)
    rid = _ask(client, "pm", "finding", f"{run_id}:1").json()["request"]["request_id"]
    assert client.post(f"/api/access-requests/{rid}/withdraw", json={}, headers=H("oncall")).status_code == 403
    assert client.post(f"/api/access-requests/{rid}/withdraw", json={},
                       headers=H("pm")).json()["request"]["status"] == "withdrawn"
    assert _decide(client, "security", rid, decision="grant").status_code == 409
    rid2 = _ask(client, "pm", "finding", f"{run_id}:1").json()["request"]["request_id"]
    assert _decide(client, "security", rid2, decision="deny").json()["request"]["status"] == "denied"
    assert client.get(f"/api/objects/finding/{run_id}:1", headers=H("pm")).json()["request"]["status"] == "denied"
    mine = client.get("/api/access-requests", headers=H("pm")).json()["requests"]
    assert [m["status"] for m in mine] == ["withdrawn", "denied"]


def test_an_approver_must_be_able_to_read_the_item(client, services):
    run_id, _, _ = _seed(client, services)
    _, art = services.orchestrator.runs.authoritative(run_id)
    art = dict(art)
    art["findings"] = [dict(f) for f in art["findings"]]
    art["findings"][1]["required_scopes"] = ["restricted:security", "staffing"]  # security lacks staffing
    services.orchestrator.runs.write_generation(run_id, art)
    rid = _ask(client, "pm", "finding", f"{run_id}:1").json()["request"]["request_id"]
    r = _decide(client, "security", rid, decision="grant")
    assert r.status_code == 403 and "cannot read" in r.json()["reason"]


def test_sensitive_kinds_owned_by_audit_need_audit(client, services):
    services.containment.trip("publication", "unsupported claim")
    assert client.get("/api/objects/kill_switch/publication", headers=H("pm")).status_code == 403
    req = _ask(client, "pm", "kill_switch", "publication").json()["request"]
    assert req["status"] == "pending" and req["owner_role"] == "audit"
    assert _decide(client, "em", req["request_id"], decision="grant").status_code == 403
    assert _decide(client, "audit", req["request_id"], decision="grant").status_code == 200
    assert client.get("/api/objects/kill_switch/publication", headers=H("pm")).status_code == 200
    # still no change allowed
    assert client.post("/api/mode", json={"mode": "offline"}, headers=H("pm")).status_code == 403


# ------------------------------------------------------------------ refusals

def test_unknown_scope_item_cannot_be_requested(client, services):
    _, task_id, _ = _seed(client, services)
    services.tasks.tasks[task_id]["required_scopes"] = None
    r = _ask(client, "pm", "task", task_id)
    assert r.status_code == 409 and r.json()["error"] == "not_requestable"
    assert client.get(f"/api/tasks/{task_id}", headers=H("pm")).json()["requestable"] is False


def test_other_project_items_and_requests_look_missing(client, services):
    _, task_id, approval_id = _seed(client, services)
    services.tasks.tasks[task_id]["project_id"] = "argo-cd"
    services.tasks.approval_rows[approval_id]["project_id"] = "argo-cd"
    assert _ask(client, "pm", "task", task_id).status_code == 404
    assert _ask(client, "pm", "task", "task-missing").status_code == 404
    assert client.get(f"/api/objects/approval/{approval_id}", headers=H("pm")).status_code == 404
    other, _ = services.access.create(project_id="argo-cd", user=None, role="pm", object_type="finding",
                                      object_id="run-x:1", label="Security finding", owner_role="security",
                                      sensitive=True, reason=None)
    assert _decide(client, "security", other["request_id"], decision="grant").status_code == 404
    assert client.get("/api/access-requests", params={"box": "to_decide"},
                      headers=H("security")).json()["requests"] == []


def test_already_open_and_bad_input(client, services):
    _, task_id, _ = _seed(client, services)
    assert _ask(client, "em", "task", task_id).json()["error"] == "already_open"
    assert _ask(client, "pm", "spaceship", "x").status_code == 400
    assert client.get("/api/access-requests", params={"box": "all"}, headers=H("pm")).status_code == 400


def test_request_is_idempotent_and_a_repeat_returns_the_same_request(client, services):
    run_id, _, _ = _seed(client, services)
    key = str(uuid.uuid4())
    body = {"object_type": "finding", "object_id": f"{run_id}:1"}
    first = client.post("/api/access-requests", json=body, headers=H("pm", key))
    replay = client.post("/api/access-requests", json=body, headers=H("pm", key))
    assert replay.headers.get("Idempotent-Replay") == "true" and replay.json() == first.json()
    again = client.post("/api/access-requests", json=body, headers=H("pm"))
    assert again.status_code == 200 and again.json()["request"]["request_id"] == first.json()["request"]["request_id"]
    assert len(client.get("/api/access-requests", headers=H("pm")).json()["requests"]) == 1


def test_pending_request_limit():
    store = AccessRequestStore()
    for n in range(MAX_PENDING_PER_USER):
        store.create(project_id="kubevirt", user="pm", role="pm", object_type="finding", object_id=f"r:{n}",
                     label="Security finding", owner_role="security", sensitive=True, reason=None)
    with pytest.raises(AccessError) as exc:
        store.create(project_id="kubevirt", user="pm", role="pm", object_type="finding", object_id="r:99",
                     label="Security finding", owner_role="security", sensitive=True, reason=None)
    assert exc.value.status == 429


def test_decisions_are_append_only_and_everything_is_audited(client, services):
    _, _, approval_id = _seed(client, services)
    req = _ask(client, "pm", "approval", approval_id).json()["request"]
    client.post(f"/api/access-grants/{req['grant']['grant_id']}/revoke", json={}, headers=H("em"))
    with pytest.raises(sqlite3.DatabaseError):
        services.access._db.execute("UPDATE access_decisions SET action = 'granted'")
    with pytest.raises(sqlite3.DatabaseError):
        services.access._db.execute("DELETE FROM access_decisions")
    actions = [d["action"] for d in services.access.get(req["request_id"])["decisions"]]
    assert actions == ["requested", "granted_automatically", "revoked"]
    audited = {e["action"] for e in services.audit.entries(req["request_id"])}
    assert {"access.request", "access.grant", "access.revoke"} <= audited


# ------------------------------------------------------------------ lists, related items, reminders

def test_lists_show_locked_placeholders_not_content(client, services):
    _, task_id, approval_id = _seed(client, services)
    tasks = client.get("/api/tasks", headers=H("pm")).json()
    assert tasks["tasks"] == []
    assert tasks["locked_items"] == [{"object_type": "task", "object_id": task_id, "label": "Task",
                                      "owner_role": "em", "needs_decision": False, "access": "locked",
                                      "via_grant": False, "requestable": True, "request": None}]
    assert len(client.get("/api/tasks", headers=H("em")).json()["tasks"]) == 1
    _ask(client, "pm", "approval", approval_id)
    approvals = client.get("/api/approvals", headers=H("pm")).json()
    assert len(approvals["approvals"]) == 1 and approvals["locked_items"] == []


def test_related_items_are_marked_open_or_locked(client, services):
    run_id, task_id, _ = _seed(client, services)
    as_pm = {(r["object_type"], r["object_id"]): r for r in
             client.get(f"/api/related/run/{run_id}", headers=H("pm")).json()["related"]}
    assert as_pm[("task", task_id)]["access"] == "locked"
    assert as_pm[("report", f"{run_id}:investigation")]["access"] == "locked"
    assert as_pm[("report", f"{run_id}:security_gate")]["needs_decision"] is True
    as_em = {(r["object_type"], r["object_id"]): r for r in
             client.get(f"/api/related/run/{run_id}", headers=H("em")).json()["related"]}
    assert as_em[("task", task_id)]["access"] == "open"
    of_task = client.get(f"/api/related/task/{task_id}", headers=H("em")).json()["related"]
    assert {"approval", "run", "report"} <= {r["object_type"] for r in of_task}
    assert client.get(f"/api/related/task/{task_id}", headers=H("pm")).json()["access"] == "locked"
    assert client.get("/api/related/task/task-missing", headers=H("pm")).status_code == 404


def test_reminder_before_a_grant_ends(client, services):
    _, _, approval_id = _seed(client, services)
    _ask(client, "pm", "approval", approval_id)
    assert sweep_expiring(services) == 0
    soon = datetime.now(timezone.utc) + timedelta(hours=23, minutes=30)
    services.access.clock = lambda: soon
    assert sweep_expiring(services) == 1 and sweep_expiring(services) == 0
    assert any(n["event_type"] == "access.expiring" for n in
               client.get("/api/notifications", headers=H("pm")).json()["notifications"])


def test_grants_do_not_widen_streams(client, services):
    run_id, _, _ = _seed(client, services)
    rid = _ask(client, "pm", "finding", f"{run_id}:1").json()["request"]["request_id"]
    _decide(client, "security", rid, decision="grant")
    services.events.emit("run.progress", run_id=run_id, authoritative_generation=1,
                         payload={"note": "SECRET-STREAM-MARKER"}, required_scopes=["restricted:security"])
    text = client.get(f"/api/runs/{run_id}/stream", params={"follow": "false"}, headers=H("pm")).text
    assert "SECRET-STREAM-MARKER" not in text
