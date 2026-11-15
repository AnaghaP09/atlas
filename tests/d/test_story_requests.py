"""Product requests and hand-offs: roles, project scope, idempotency, notifications and home figures."""

from __future__ import annotations

import pytest

from tests.d.conftest import H


def _request(client, kind="technical_dependency", role="pm", key=None, **extra):
    body = {"kind": kind, "title": "Shared cache for the API", "description": "We need a cache before the next release.",
            **extra}
    return client.post("/api/requests", json=body, headers=H(role, key))


def _notes(services, event):
    return [r for r in services.notifier.records() if r["event_type"] == event]


def test_only_the_product_manager_files_a_request(client):
    for role in ("em", "architect", "security", "oncall", "audit"):
        assert _request(client, role=role).status_code == 403, role
    assert _request(client).status_code == 201


@pytest.mark.parametrize("kind,owner,info", [("technical_dependency", "architect", "em"), ("new_feature", "em", "architect")])
def test_the_kind_picks_the_owner_and_the_other_role_is_informed(client, services, kind, owner, info):
    r = _request(client, kind=kind).json()["request"]
    assert (r["owner_role"], r["info_role"], r["status"]) == (owner, info, "new")
    assert [n["roles"] for n in _notes(services, "request.created")] == [[owner]]
    assert [n["roles"] for n in _notes(services, "request.shared")] == [[info]]
    queue = [e for e in services.queue.entries() if e["trigger"] == "product_request"]
    assert len(queue) == 1 and queue[0]["project_id"] == "kubevirt"
    for role, owned, fyi in ((owner, 1, 0), (info, 0, 1)):
        home = client.get("/api/story/home", headers=H(role)).json()["requests"]
        assert (home["new"], home["owned"], home["for_information"]) == (1, owned, fyi)
        assert home["items"][0]["request_id"] == r["request_id"]


def test_invalid_requests_are_refused(client):
    assert _request(client, kind="other").status_code == 400
    assert client.post("/api/requests", json={"kind": "new_feature", "title": "", "description": "x"},
                       headers=H("pm")).status_code == 400
    assert _request(client, link_type="component").status_code == 400
    assert _request(client, link_type="component", link_id="virt-handler").status_code == 201


def test_a_repeated_key_files_one_request(client, services):
    first = _request(client, key="3f2b8c1e-0d4a-4b7e-9a51-2c6f8e9d1a01")
    again = _request(client, key="3f2b8c1e-0d4a-4b7e-9a51-2c6f8e9d1a01")
    assert first.status_code == 201 and again.status_code == 201
    assert again.headers.get("Idempotent-Replay") == "true"
    assert len(services.story.requests()) == 1


def test_only_the_owner_answers_and_the_product_manager_is_told(client, services):
    rid = _request(client).json()["request_id"]
    answer = {"status": "accepted", "reply": "We will add it to the next plan."}
    for role in ("em", "pm", "audit"):
        assert client.post(f"/api/requests/{rid}/answer", json=answer, headers=H(role)).status_code == 403, role
    assert client.post(f"/api/requests/{rid}/answer", json={"status": "new", "reply": "x"},
                       headers=H("architect")).status_code == 400
    assert client.post(f"/api/requests/{rid}/answer", json={"status": "declined", "reply": ""},
                       headers=H("architect")).status_code == 400
    r = client.post(f"/api/requests/{rid}/answer", json=answer, headers=H("architect"))
    assert r.status_code == 200 and r.json()["request"]["status"] == "accepted"
    told = _notes(services, "request.answered")
    assert len(told) == 1 and told[0]["roles"] == ["pm"]
    mine = client.get("/api/story/home", headers=H("pm")).json()["my_requests"]
    assert mine["items"][0]["status"] == "accepted" and mine["open"] == 0
    assert any(e["action"] == "request.accepted" for e in services.audit.entries(rid))


def test_another_projects_request_answers_like_a_missing_one(client, services):
    other = services.story.add_request(project_id="argo-cd", kind="new_feature", title="t", description="d",
                                       link_type=None, link_id=None, link_label=None, from_role="pm", from_user=None)
    rid = other["request_id"]
    assert client.get(f"/api/requests/{rid}", headers=H("em")).status_code == 404
    assert client.post(f"/api/requests/{rid}/answer", json={"status": "accepted", "reply": "ok"},
                       headers=H("em")).status_code == 404
    assert client.get("/api/requests", headers=H("em")).json()["requests"] == []
    assert client.get("/api/story/home", headers=H("em")).json()["requests"]["new"] == 0


def test_roles_outside_the_story_cannot_read_requests(client):
    for role in ("security", "oncall"):
        assert client.get("/api/requests", headers=H(role)).status_code == 403


# ------------------------------------------------------------------ hand-offs

def _handoff(client, to_role="em", role="pm", key=None, **extra):
    body = {"subject_type": "forecast_item", "subject_id": "issue-123", "subject_title": "Flaky IPv6 tests",
            "to_role": to_role, "note": "Please plan this for the next release.", **extra}
    return client.post("/api/handoffs", json=body, headers=H(role, key))


def test_the_product_manager_hands_an_item_to_the_engineering_manager(client, services):
    for role in ("em", "architect", "audit"):
        assert _handoff(client, role=role).status_code == 403
    r = _handoff(client)
    assert r.status_code == 201
    hid = r.json()["handoff_id"]
    assert [n["roles"] for n in _notes(services, "handoff.created")] == [["em"]]
    assert any(e["trigger"] == "handoff" and e["subject_id"] == hid for e in services.queue.entries())
    home = client.get("/api/story/home", headers=H("em")).json()
    assert home["handoffs_waiting"] == 1 and home["handoffs"][0]["handoff_id"] == hid
    assert client.get("/api/story/home", headers=H("architect")).json()["handoffs_waiting"] == 0
    assert any(e["action"] == "handoff.create" for e in services.audit.entries(hid))


def test_a_hand_off_to_the_architect_and_its_answer(client, services):
    hid = _handoff(client, to_role="architect").json()["handoff_id"]
    assert client.post(f"/api/handoffs/{hid}/answer", json={"status": "accepted"}, headers=H("em")).status_code == 403
    r = client.post(f"/api/handoffs/{hid}/answer", json={"status": "accepted", "reply": "Taking it."},
                    headers=H("architect"))
    assert r.status_code == 200 and r.json()["handoff"]["status"] == "accepted"
    assert _notes(services, "handoff.answered")[0]["roles"] == ["pm"]
    assert client.get("/api/story/home", headers=H("architect")).json()["handoffs_waiting"] == 0


def test_invalid_hand_offs_are_refused(client):
    assert _handoff(client, to_role="security").status_code == 400
    assert _handoff(client, subject_type="other").status_code == 400
    assert _handoff(client, note="").status_code == 400
    assert _handoff(client, subject_type="investigation").status_code == 400  # needs its run
    assert _handoff(client, subject_type="investigation", run_id="run-missing").status_code == 404


def test_a_repeated_key_records_one_hand_off(client, services):
    assert _handoff(client, key="3f2b8c1e-0d4a-4b7e-9a51-2c6f8e9d1a02").status_code == 201
    assert _handoff(client, key="3f2b8c1e-0d4a-4b7e-9a51-2c6f8e9d1a02").headers.get("Idempotent-Replay") == "true"
    assert len(services.story.handoffs()) == 1


def test_requests_page_is_on_the_menu_of_the_three_main_roles(client):
    for role in ("pm", "em", "architect"):
        pages = [p["page_id"] for g in client.get("/api/navigation", headers=H(role)).json()["groups"] for p in g["pages"]]
        assert "requests" in pages, role


@pytest.mark.parametrize("decision", ["coding_agent", "oncall"])
def test_the_engineering_manager_delegates_a_hand_off(client, services, decision):
    hid = _handoff(client).json()["handoff_id"]
    bad = client.post(f"/api/handoffs/{hid}/answer", json={"status": "accepted", "decision": "someone"}, headers=H("em"))
    assert bad.status_code == 400
    r = client.post(f"/api/handoffs/{hid}/answer", json={"status": "accepted", "decision": decision}, headers=H("em"))
    assert r.status_code == 200 and r.json()["handoff"]["decision"] == decision
    seen = client.get(f"/api/handoffs/{hid}", headers=H("pm")).json()["handoff"]
    assert seen["decision"] == decision and seen["status"] == "accepted"
    text = _notes(services, "handoff.answered")[0]["content"]
    assert "Delegated to" in str(text)


def test_only_the_engineering_manager_delegates(client):
    hid = _handoff(client, to_role="architect").json()["handoff_id"]
    r = client.post(f"/api/handoffs/{hid}/answer", json={"status": "accepted", "decision": "oncall"},
                    headers=H("architect"))
    assert r.status_code == 400
