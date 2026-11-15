"""Automatic runs: the real watchers show up in the schedules API and behave safely."""

from __future__ import annotations


from atlas.contracts import Mode
from atlas.watchers import SchedulerLoop

from tests.gaps.conftest import BASE, headers as _h


def _schedules(client, role="em") -> dict[str, dict]:
    body = client.get("/api/schedules", headers={"X-Atlas-Role": role}).json()
    return {s["schedule_id"]: s for s in body["schedules"]}


def test_every_automatic_run_is_listed_with_its_state(server):
    services, client, _ = server
    rows = _schedules(client)
    assert set(rows) == {"dashboard:alpha", "release:alpha", "failure:alpha", "nightly:alpha", "housekeeping"}
    for sid in ("release:alpha", "failure:alpha", "nightly:alpha"):
        assert rows[sid]["state"] == "paused" and rows[sid]["spends_model_budget"] is True
        assert "person turns it on" in rows[sid]["reason"] and rows[sid]["next_run"] is None
    assert rows["dashboard:alpha"]["state"] == "enabled" and rows["dashboard:alpha"]["next_run"]
    assert rows["housekeeping"]["state"] == "enabled" and rows["housekeeping"]["interval_seconds"] == 300
    assert all(r["title"] for r in rows.values())


def test_nothing_that_spends_runs_until_a_person_enables_it(server):
    services, client, _ = server
    services.feed.update_panels("alpha", {"latest_release": [{"tag": "v1.0.0"}]})
    SchedulerLoop(services).run_once()
    services.feed.update_panels("alpha", {"latest_release": [{"tag": "v1.1.0"}]})
    SchedulerLoop(services).run_once()
    assert services.admissions.for_project("alpha") == []
    assert _schedules(client)["release:alpha"]["last_run"] is None


def test_release_watcher_admits_one_release_close_per_new_release(server):
    services, client, _ = server
    services.feed.update_panels("alpha", {"latest_release": [{"tag": "v1.0.0"}]})
    assert client.post("/api/schedules/release:alpha/enable", headers=_h("em"), json={}).status_code == 200
    services.scheduler.watchers["release:alpha"].interval_seconds = 0
    loop = SchedulerLoop(services)
    loop.run_once()  # first look: notes v1.0.0, starts nothing
    assert services.admissions.for_project("alpha") == []
    services.feed.update_panels("alpha", {"latest_release": [{"tag": "v1.1.0"}]})
    loop.run_once()
    loop.run_once()  # the same release again is not a new event
    runs = services.admissions.for_project("alpha")
    assert len(runs) == 1
    assert runs[0].trigger["trigger"] == "release_close" and runs[0].trigger["release_id"] == "v1.1.0"
    assert runs[0].snapshot_id.startswith("alpha.") and runs[0].started_by["kind"] == "watcher"
    row = _schedules(client)["release:alpha"]
    assert row["state"] == "enabled" and row["last_event_id"] == "release:v1.1.0" and row["last_run"]


def test_failure_watcher_investigates_only_issues_reported_after_it_started(server):
    services, client, _ = server
    services.feed.update_panels("alpha", {"open_issues": [{"number": 7, "title": "old"}]})
    client.post("/api/schedules/failure:alpha/enable", headers=_h("em"), json={})
    services.scheduler.watchers["failure:alpha"].interval_seconds = 0
    loop = SchedulerLoop(services)
    loop.run_once()
    services.feed.update_panels("alpha", {"open_issues": [{"number": 7, "title": "old"}, {"number": 9, "title": "new"}]})
    loop.run_once()
    loop.run_once()
    runs = services.admissions.for_project("alpha")
    assert [r.trigger["event_id"] for r in runs] == ["9"]
    assert runs[0].trigger["trigger"] == "failure"


def test_run_once_records_the_person_and_pause_stops_the_watcher(server):
    services, client, _ = server
    services.feed.update_panels("alpha", {"latest_release": [{"tag": "v1.0.0"}]})
    client.post("/api/schedules/release:alpha/run-once", headers=_h("em"), json={})  # first look
    services.feed.update_panels("alpha", {"latest_release": [{"tag": "v2.0.0"}]})
    out = client.post("/api/schedules/release:alpha/run-once", headers=_h("em"), json={})
    assert out.status_code == 200 and len(out.json()["admitted"]) == 1
    run = services.admissions.for_project("alpha")[0]
    assert run.started_by == {"kind": "person", "role": "em", "watcher_id": "release:alpha"}
    assert _schedules(client)["release:alpha"]["state"] == "paused"  # run-once does not turn it on
    assert client.post("/api/schedules/release:alpha/pause", headers=_h("pm"), json={}).status_code == 403


def test_offline_pauses_external_watchers_with_a_reason(server):
    services, client, _ = server
    client.post("/api/schedules/release:alpha/enable", headers=_h("em"), json={})
    services.mode.set(Mode.OFFLINE)
    rows = _schedules(client)
    for sid in ("dashboard:alpha", "release:alpha", "failure:alpha"):
        assert rows[sid]["state"] == "paused" and rows[sid]["reason"].startswith("offline")
        assert rows[sid]["next_run"] is None
    assert rows["housekeeping"]["state"] == "enabled"
    result = services.scheduler.tick()
    assert set(result.skipped) >= {"dashboard:alpha", "release:alpha", "failure:alpha"}
    assert "housekeeping" not in result.skipped


def test_housekeeping_runs_on_its_interval_and_shows_its_last_run(server):
    services, client, sweeps = server
    loop = SchedulerLoop(services)
    loop.run_once()
    loop.run_once()  # not due again inside the interval
    assert sweeps == ["ran"]
    row = _schedules(client)["housekeeping"]
    assert row["last_run"] and row["next_run"] > row["last_run"]
    client.post("/api/schedules/housekeeping/run-once", headers=_h("audit"), json={})
    assert sweeps == ["ran", "ran"]


def test_nightly_sweep_queues_at_most_ten_uninvestigated_issues(server):
    services, client, _ = server
    from atlas.watchers import nightly_watcher
    from datetime import datetime, timezone

    noon = lambda: datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)  # noqa: E731
    services.feed.update_panels("alpha", {"open_issues": [{"number": n, "title": f"issue {n}"} for n in range(1, 13)]})
    watcher = nightly_watcher(services, "alpha", "02:00", noon)
    events = list(watcher.poll(None))
    assert events[0].trigger["trigger"] == "nightly"
    assert len(events[0].trigger["queued"]) == 10 and events[0].trigger["carried_over"] == 2
    assert list(watcher.poll(events[0].position)) == []
    early = nightly_watcher(services, "alpha", "23:00", noon)
    assert list(early.poll(None)) == []
