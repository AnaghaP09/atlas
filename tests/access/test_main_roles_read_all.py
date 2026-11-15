"""The product manager, engineering manager and solution architect read every item and page in their
project without asking. Actions keep their own role rules, and the other roles keep their locks."""

from __future__ import annotations

import pytest

from atlas.access import ALL_ROLES, KINDS, policy_for
from atlas.api import navigation as nav
from tests.d.conftest import H

MAIN = ("pm", "em", "architect")


def _seed(client, services):
    """An investigation with a task and its approval, a release close, a question and a tripped switch."""
    services.orchestrator.action = "DELEGATE_TO_CODING_AGENT"
    inv = client.post("/api/triggers/manual/investigate", json={"snapshot_id": "snap-v1.9.0", "event_id": "G01"},
                      headers=H("em")).json()["run_id"]
    task_id = client.post("/api/tasks", json={"run_id": inv}, headers=H("em")).json()["task"]["task_id"]
    approval_id = next(iter(services.tasks.approval_rows))
    close = client.post("/api/triggers/release-close", json={"snapshot_id": "snap-v1.9.0", "release_id": "v1.9.0"},
                        headers=H("pm")).json()["run_id"]
    question = client.post("/api/triggers/question", json={"snapshot_id": "snap-v1.9.0", "question": "What slipped?"},
                           headers=H("pm")).json()["run_id"]
    incident_id = services.containment.trip("publication", "unsupported claim")
    services.tasks.scorecard = lambda project_id=None: {"rows": [{"row_id": "row-1", "task_id": task_id,
                                                                   "required_scopes": ["internal"]}]}
    return {"inv": inv, "task": task_id, "approval": approval_id, "close": close, "question": question,
            "incident": incident_id}


# ------------------------------------------------------------------ the table and the menu

@pytest.mark.parametrize("role", MAIN)
@pytest.mark.parametrize("kind", sorted(KINDS))
def test_every_kind_lists_the_main_roles_as_readers(role, kind):
    object_type, _, subtype = kind.partition(":")
    for scopes in (["internal"], ["internal", "restricted:security"]):
        assert role in policy_for(object_type, subtype or None, scopes).readers


@pytest.mark.parametrize("role", MAIN)
def test_main_roles_get_every_page(role):
    pages = {p["page_id"] for g in nav.navigation_for(role)["groups"] for p in g["pages"]}
    assert pages == {p.page_id for p in nav.PAGES}
    assert nav.navigation_for(role)["other_pages"] == []


@pytest.mark.parametrize("role", sorted(ALL_ROLES))
def test_every_role_sees_the_forecast_page(role):
    assert "report_forecast" in nav.ROLE_PAGES[role]


# ------------------------------------------------------------------ reading through the API

@pytest.mark.parametrize("role", MAIN)
def test_main_roles_read_every_object_type(client, services, role):
    ids = _seed(client, services)
    inv, close = ids["inv"], ids["close"]
    reports = [f"{close}:{t}" for t in ("forecast", "allocation", "architecture", "security_gate")]
    reports += [f"{inv}:investigation", f"{inv}:security_gate", f"{ids['question']}:question"]
    for report_id in reports:
        r = client.get(f"/api/reports/{report_id}", headers=H(role))
        assert r.status_code == 200, (report_id, r.text)
        assert r.json()["read_only"] is False and r.json()["withheld"] == 0
    objects = [("run", inv), ("finding", f"{inv}:1"), ("evidence", f"{inv}::rec-restricted"),
               ("task", ids["task"]), ("approval", ids["approval"]), ("scorecard_row", "row-1"),
               ("graph", "snap-v1.9.0"), ("incident", ids["incident"]), ("kill_switch", "publication"),
               ("dead_letter", "list")] + [("report", r) for r in reports]
    for object_type, object_id in objects:
        r = client.get(f"/api/objects/{object_type}/{object_id}", headers=H(role))
        assert r.status_code == 200, (object_type, r.text)
        assert r.json().get("via_grant") in (None, False)
    finding = client.get(f"/api/objects/finding/{inv}:1", headers=H(role))
    assert "SECRET-FINDING-MARKER" in finding.text
    direct = [f"/api/tasks/{ids['task']}", f"/api/approvals/{ids['approval']}", "/api/notifications/dead-letter",
              "/api/graph/snap-v1.9.0", f"/api/runs/{inv}"]
    for path in direct:
        assert client.get(path, headers=H(role)).status_code == 200, path
    ev = client.get("/api/evidence/rec-restricted", params={"run_id": inv}, headers=H(role))
    assert ev.status_code == 200 and "SECRET-FINDING-MARKER" in ev.text
    assert client.get("/api/tasks", headers=H(role)).json()["locked_items"] == []
    for subject in (("run", inv), ("run", close), ("task", ids["task"])):
        related = client.get(f"/api/related/{subject[0]}/{subject[1]}", headers=H(role)).json()["related"]
        assert related and all(r["access"] == "open" for r in related), related


@pytest.mark.parametrize("role", sorted(ALL_ROLES))
def test_every_role_reads_the_forecast_and_its_evidence(client, services, role):
    ids = _seed(client, services)
    r = client.get(f"/api/reports/{ids['close']}:forecast", headers=H(role))
    assert r.status_code == 200, r.text
    ev = client.get("/api/evidence/rec-public", params={"run_id": ids["close"]}, headers=H(role))
    assert ev.status_code == 200 and ev.json()["record"]["record_id"] == "rec-public"


def test_beta_roles_keep_their_locks(client, services):
    ids = _seed(client, services)
    locked = client.get(f"/api/reports/{ids['close']}:security_gate", headers=H("oncall"))
    assert locked.status_code == 403 and locked.json()["error"] == "locked"
    assert client.get(f"/api/reports/{ids['close']}:allocation", headers=H("oncall")).status_code == 403


# ------------------------------------------------------------------ actions keep their role rules

@pytest.mark.parametrize("role", ["pm", "architect"])
def test_pm_and_architect_cannot_act_for_the_engineering_manager(client, services, role):
    ids = _seed(client, services)
    before = list(services.tasks.calls)
    assert client.post(f"/api/approvals/{ids['approval']}/decision", json={"decision": "approve"},
                       headers=H(role)).status_code == 403
    assert client.post("/api/tasks", json={"run_id": ids["inv"]}, headers=H(role)).status_code == 403
    assert client.post("/api/schedules/any/pause", json={}, headers=H(role)).status_code == 403
    assert client.post(f"/api/runs/{ids['inv']}/reverify", json={}, headers=H(role)).status_code == 403
    assert services.tasks.calls == before


def test_em_cannot_act_for_audit(client, services):
    ids = _seed(client, services)
    assert client.post("/api/mode", json={"mode": "offline"}, headers=H("em")).status_code == 403
    assert client.post(f"/api/incidents/{ids['incident']}/resume", json={"prerequisite_ids": ["x"]},
                       headers=H("em")).status_code == 403
    assert client.post("/api/projects", json={"repository": "https://github.com/argoproj/argo-cd"},
                       headers=H("em")).status_code == 403


def test_architect_cannot_start_a_release_close(client, services):
    r = client.post("/api/triggers/release-close", json={"snapshot_id": "snap-v1.9.0", "release_id": "v1.9.0"},
                    headers=H("architect"))
    assert r.status_code == 403
