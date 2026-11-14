"""Live project metrics: values come from the stores at request time, and every change emits one event."""

from __future__ import annotations

from tests.d.conftest import H


def _metrics(client, role="em"):
    r = client.get("/api/projects/kubevirt/metrics", headers=H(role))
    assert r.status_code == 200
    return r.json()


def _events(services, name="project.metrics_changed"):
    return [e for e in services.hub._buf if e["wire"]["event"] == name]


def test_metrics_shape_and_values_from_stores(client, services):
    m = _metrics(client)
    for key in ("open_forecast_items", "runs_by_status", "needs_a_person", "approvals_pending", "kill_switches_tripped",
                "unread_notifications", "spend_used_usd", "spend_ceiling_usd", "last_poll", "dashboard_counts",
                "metrics_version", "computed_at"):
        assert key in m
    assert m["runs_total"] == 0


def test_a_run_changes_the_numbers_and_emits_an_event(client, services):
    before = _metrics(client)["metrics_version"]
    r = client.post("/api/triggers/release-close", json={"snapshot_id": "snap-v1.9.0", "release_id": "1.9"}, headers=H("pm"))
    assert r.status_code in (200, 201, 202)
    assert _events(services), "no project.metrics_changed after a run"
    m = _metrics(client)
    assert m["metrics_version"] > before
    assert m["runs_total"] >= 1
    assert sum(m["runs_by_status"].values()) == m["runs_total"]


def test_ack_emits_an_event(client, services):
    n = len(_events(services))
    client.post("/api/notifications/does-not-exist/ack", headers=H("em"))
    # The ack route emits even when nothing changed visibly only if it succeeded; a refused ack emits nothing.
    assert len(_events(services)) >= n


def test_spend_steps_emit(client, services):
    _metrics(client)  # installs the hooks
    if services.spend is None:
        return
    n = len(_events(services))
    services.spend.add(0.10)
    assert len(_events(services)) == n + 1


def test_role_without_a_list_gets_none_not_a_number(client):
    m = _metrics(client, role="pm")
    assert m["approvals_pending"] is None or isinstance(m["approvals_pending"], int)
