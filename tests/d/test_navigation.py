"""Role-based navigation: each role gets exactly its pages, and the menu agrees with the trigger catalog."""

from __future__ import annotations

import pytest

from atlas.api import navigation as nav
from atlas.api.catalog import MANUAL_TRIGGERS
from tests.d.conftest import H

EXPECTED = {
    "pm": {"home", "ship_readiness", "dashboard", "report_forecast", "run_now", "runs", "feedback", "evaluation",
           "notifications", "access_requests"},
    "em": {"home", "ship_readiness", "dashboard", "report_allocation", "queue", "approvals", "tasks", "schedules",
           "run_now", "runs", "scorecard", "notifications", "access_requests"},
    "architect": {"home", "ship_readiness", "dashboard", "report_architecture", "graph", "runs", "feedback",
                  "notifications", "access_requests"},
    "security": {"home", "dashboard", "report_security", "queue", "containment", "notifications", "access_requests"},
    "oncall": {"home", "dashboard", "run_now", "runs", "report_investigation", "notifications", "access_requests"},
    "audit": {p.page_id for p in nav.PAGES} - {"ship_readiness"},
}


def _pages(body):
    return {p["page_id"] for g in body["groups"] for p in g["pages"]}


@pytest.mark.parametrize("role", sorted(EXPECTED))
def test_each_role_gets_exactly_its_pages(client, role):
    r = client.get("/api/navigation", headers=H(role))
    assert r.status_code == 200
    body = r.json()
    assert _pages(body) == EXPECTED[role]
    assert body["home"] == f"{role}_home"
    hidden = {p["page_id"] for p in body["other_pages"]}
    assert hidden == {p.page_id for p in nav.PAGES} - EXPECTED[role]
    assert all(p["for"] for g in body["groups"] for p in g["pages"])


def test_hidden_pages_name_the_roles_they_are_for(client):
    body = client.get("/api/navigation", headers=H("pm")).json()
    approvals = next(p for p in body["other_pages"] if p["page_id"] == "approvals")
    assert "em" in approvals["roles"] and "engineering manager" in approvals["role_titles"]


def test_everyone_sees_home_and_dashboard():
    for role in nav.ROLES:
        assert {"home", "dashboard"} <= set(nav.ROLE_PAGES[role])


def test_run_now_only_for_roles_that_may_start_a_trigger():
    trigger_roles = {r for t in MANUAL_TRIGGERS for r in t.roles}
    for role, pages in nav.ROLE_PAGES.items():
        if "run_now" in pages:
            assert role in trigger_roles


def test_matrix_check_catches_drift(monkeypatch):
    monkeypatch.setitem(nav.ROLE_PAGES, "security", nav.ROLE_PAGES["security"] + ("run_now", "nope"))
    with pytest.raises(ValueError):
        nav.check_matrix()


def test_navigation_needs_an_identity(client):
    assert client.get("/api/navigation").status_code == 401
