"""The release outlook: Q1 from the latest verified release close, Q2 from live inputs with explicit status rules."""

from __future__ import annotations

import pytest

from atlas.api import outlook as ol
from atlas.feed import ProjectFeed
from atlas.projects import Project, ProjectRegistry
from tests.d.conftest import H

URL = "/api/projects/kubevirt/outlook"


def _get(client, role="pm"):
    r = client.get(URL, headers=H(role))
    assert r.status_code == 200, r.text
    return r.json()


def _feed(services) -> ProjectFeed:
    if services.feed is None:
        services.feed = ProjectFeed(services.hub)
    return services.feed


def _live(services, *, failing=0, blocking=0, issues=()):
    feed = _feed(services)
    jobs = [{"job": f"periodic-e2e-sig-compute-{n}", "result": "FAILURE"} for n in range(failing)]
    sec = [{"note": f"10 modules scanned at 2026-11-15T09:36+00:00: {blocking} finding(s), {blocking} blocking"}]
    sec += [{"finding_id": f"f{n}", "blocking": True, "required_scopes": ["restricted:security"]} for n in range(blocking)]
    feed.update_panels("kubevirt", {"failing_ci": jobs, "security": sec, "open_issues": list(issues)},
                       totals={"failing_ci": failing, "security": blocking})


@pytest.fixture
def configured(services):
    services.projects = ProjectRegistry({"kubevirt": Project("kubevirt", name="KubeVirt",
                                                             labels={"critical": "priority/critical-urgent"})})
    return services


def _close(client, services, items):
    r = client.post("/api/triggers/release-close", json={"snapshot_id": "snap-v1.9.0", "release_id": "1.9"},
                    headers=H("pm"))
    assert r.status_code == 202
    run_id = r.json()["run_id"]
    _, art = services.orchestrator.runs.authoritative(run_id)
    art = dict(art, forecast={"items": items, "date_range": "illustrative"},
               verifier={"claims_checked": len(items), "claims_rejected": 0})
    services.orchestrator.runs.write_generation(run_id, art)
    return run_id


ITEMS = [
    {"id": "item-1", "issue_id": "1", "title": "flaky VSOCK test", "component": "compute", "score": 4.1,
     "allocation": "coding_agent", "allocation_reason": "One flaky test, one component", "owner_team": "sig/compute",
     "evidence": ["issues:issue/1@a"], "required_scopes": ["public"]},
    {"id": "item-2", "issue_id": "2", "title": "s390x lane", "component": "compute", "score": 4.0,
     "allocation": "developer", "allocation_reason": "Needs special hardware", "evidence": ["issues:issue/2@a"],
     "required_scopes": ["public"]},
    {"id": "item-3", "issue_id": "3", "title": "blocker", "component": "storage", "score": 3.9,
     "allocation": "decision_maker", "allocation_reason": "Marked as a release blocker", "evidence": [],
     "required_scopes": ["public"]},
    {"id": "item-4", "issue_id": "4", "title": "hidden", "component": "network", "score": 3.0,
     "allocation": "developer", "evidence": [], "required_scopes": ["restricted:security"]},
]


# ------------------------------------------------------------------ Q1


def test_no_release_close_says_so_with_a_run_action(client, configured):
    q1 = _get(client)["q1"]
    assert q1["state"] == "none"
    assert q1["message"] == "No release close yet"
    assert q1["run_action"]["allowed"] is True
    assert q1["total"] == 0


def test_run_action_is_off_for_a_role_that_may_not_start_it(client, configured):
    assert _get(client, "architect")["q1"]["run_action"]["allowed"] is False


def test_q1_reads_the_latest_verified_close_and_splits_owners(client, configured):
    run_id = _close(client, configured, ITEMS)
    q1 = _get(client)["q1"]
    assert q1["state"] == "ready" and q1["run_id"] == run_id
    assert q1["total"] == 4 and q1["withheld"] == 0  # the main roles read restricted items too
    assert q1["split"] == {"developers": 2, "coding_agents": 1, "decide_first": 1, "not_allocated": 0}
    assert q1["date_range"] == "illustrative" and q1["basis"] == "predicted"
    first = q1["items"][0]
    assert first["evidence_link"].startswith("/evidence/") and first["reason"] and first["owner_team"] == "sig/compute"
    assert q1["dataset"]["snapshot_id"] == "snap-v1.9.0"
    assert "confidence" not in q1


def test_q1_report_links_say_who_can_open_them(client, configured):
    _close(client, configured, ITEMS)
    assert _get(client, "pm")["q1"]["report_access"] == "open"
    assert _get(client, "em")["q1"]["report_access"] == "open"
    assert _get(client, "em")["q1"]["allocation_access"] == "open"


# ------------------------------------------------------------------ Q2 status rules


def test_not_measured_when_inputs_are_missing(client, configured):
    q2 = _get(client)["q2"]
    assert q2["status"] == "NOT_MEASURED"
    assert {m["input"] for m in q2["missing_inputs"]} >= {"failing_ci", "security_gate", "critical_issues"}
    assert q2["reasons"][0]["rule"] == "inputs_missing"


def test_on_track_when_every_input_is_clear(client, configured):
    _live(configured)
    q2 = _get(client)["q2"]
    assert q2["status"] == "ON_TRACK", q2["reasons"]
    assert q2["blocker_count"] == 0 and q2["service_count"] == 0


def test_at_risk_from_failing_ci_threshold(client, configured):
    _live(configured, failing=ol.AT_RISK_FAILING_CI - 1)
    assert _get(client)["q2"]["status"] == "ON_TRACK"
    _live(configured, failing=ol.AT_RISK_FAILING_CI)
    q2 = _get(client)["q2"]
    assert q2["status"] == "AT_RISK"
    assert q2["reasons"][0]["rule"] == "failing_ci"
    assert q2["affected_services"][0]["component"] == "compute"
    assert q2["affected_services"][0]["blockers"] == ol.AT_RISK_FAILING_CI


def test_at_risk_from_a_critical_issue(client, configured):
    _live(configured, issues=[{"number": 7, "title": "boom", "labels": ["priority/critical-urgent", "sig/storage"]},
                              {"number": 8, "title": "calm", "labels": ["kind/bug"]}])
    q2 = _get(client)["q2"]
    assert q2["status"] == "AT_RISK"
    assert q2["inputs"]["critical_issues"]["count"] == 1
    assert [s["component"] for s in q2["affected_services"]] == ["storage"]


def test_blocked_by_the_security_gate(client, configured):
    _live(configured, blocking=2)
    q2 = _get(client)["q2"]
    assert q2["status"] == "BLOCKED"
    assert q2["inputs"]["security_gate"] == {"state": "blocked", "blocking_findings": 2, "findings": 2,
                                             "link": "/dashboard#security", "note": q2["inputs"]["security_gate"]["note"]}
    assert q2["reasons"][0]["rule"] == "security_gate_blocked"
    assert "f0" not in str(q2), "finding details stay with the security role"


def test_blocked_by_an_escalated_investigation_until_a_decision(client, configured):
    _live(configured)
    configured.orchestrator.action = "ESCALATE_DECISION"
    r = client.post("/api/triggers/manual/investigate", json={"snapshot_id": "snap-v1.9.0", "event_id": "42",
                                                              "components": ["network"]}, headers=H("em"))
    assert r.status_code == 202
    run_id = r.json()["run_id"]
    q2 = _get(client, "architect")["q2"]
    assert q2["status"] == "BLOCKED"
    assert q2["reasons"][0]["rule"] == "decision_waiting"
    assert q2["blockers"][0]["link"] == f"/runs/{run_id}"
    configured.feedback.record_feedback(run_id, "override", "action", "ship", "accepted for this release", "em")
    assert _get(client)["q2"]["status"] == "ON_TRACK"


def test_held_investigation_is_at_risk(client, configured, monkeypatch):
    _live(configured)
    r = client.post("/api/triggers/manual/investigate", json={"snapshot_id": "snap-v1.9.0", "event_id": "43"},
                    headers=H("em"))
    run_id = r.json()["run_id"]
    _, art = configured.orchestrator.runs.authoritative(run_id)
    configured.orchestrator.runs.write_generation(run_id, dict(art, safety_disposition="HOLD_FOR_HUMAN",
                                                               action={"value": "REJECT", "claim_ids": [],
                                                                       "required_scopes": ["public"]}))
    q2 = _get(client)["q2"]
    assert q2["status"] == "AT_RISK" and q2["reasons"][0]["rule"] == "held_for_person"


def test_blocked_wins_over_at_risk_with_every_reason_listed(client, configured):
    _live(configured, failing=5, blocking=1)
    q2 = _get(client)["q2"]
    assert q2["status"] == "BLOCKED"
    assert [r["rule"] for r in q2["reasons"]] == ["security_gate_blocked", "failing_ci"]


# ------------------------------------------------------------------ roles and events


@pytest.mark.parametrize("role", ["pm", "em", "architect"])
def test_main_roles_get_both_questions(client, configured, role):
    body = _get(client, role)
    assert body["sections"] == ["q1", "q2"]
    assert body["q1"] is not None and body["q2"] is not None
    assert isinstance(body["outlook_version"], int)


@pytest.mark.parametrize("role", ["security", "oncall", "audit"])
def test_beta_roles_get_no_outlook(client, configured, role):
    r = client.get(URL, headers=H(role))
    assert r.status_code == 403


def test_navigation_names_sections_and_beta(client):
    nav = client.get("/api/navigation", headers=H("pm")).json()
    assert nav["outlook"] == ["q1", "q2"] and nav["beta"] is False
    assert any(p["page_id"] == "ship_readiness" for g in nav["groups"] for p in g["pages"])
    sec = client.get("/api/navigation", headers=H("security")).json()
    assert sec["outlook"] == [] and sec["beta"] is True
    assert not any(p["page_id"] == "ship_readiness" for g in sec["groups"] for p in g["pages"])


def test_graph_links_for_every_main_role(client, configured):
    _live(configured, failing=3)
    arch = _get(client, "architect")["q2"]["affected_services"][0]
    pm = _get(client, "pm")["q2"]["affected_services"][0]
    assert arch["link"] == "/graph" and pm["link"] == "/graph"


def _outlook_events(services):
    return [e for e in services.hub._buf if e["wire"]["event"] == "project.outlook_changed"]


def test_inputs_changing_emit_an_outlook_event(client, configured):
    v0 = _get(client)["outlook_version"]
    _live(configured, failing=1)
    events = _outlook_events(configured)
    assert events and events[-1]["wire"]["data"]["changed"] == ["q2"]
    assert events[-1]["project_id"] == "kubevirt"
    _close(client, configured, ITEMS)
    assert _get(client)["outlook_version"] > v0
    assert "q1" in _outlook_events(configured)[-1]["wire"]["data"]["changed"]


def test_job_component_mapping():
    assert ol.job_component("periodic-kubevirt-e2e-k8s-1.37-sig-compute-root") == "compute"
    assert ol.job_component("periodic-kubevirt-e2e-k8s-1.36-sig-storage") == "storage"
    assert ol.job_component("periodic-kubevirt-e2e-test-s390x") is None
