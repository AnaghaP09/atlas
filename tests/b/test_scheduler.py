from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest

from atlas.contracts import Mode, Role, StartedBy, StartedByKind
from atlas.scheduler import (
    ADMITTED,
    DISPATCHED,
    HELD,
    AdmissionStore,
    Dispatcher,
    ObservedEvent,
    Scheduler,
    Watcher,
    manual_admission,
    nightly_due,
)


class FeedSource:
    """A failure feed: events after the cursor position."""

    def __init__(self, events):
        self.events = events

    def __call__(self, cursor):
        start = int(cursor) if cursor is not None else 0
        return [e for e in self.events if int(e.position) > start]


def ev(n: int, attempt: int = 1) -> ObservedEvent:
    return ObservedEvent(key=f"job-{n}#{attempt}", position=str(n), trigger={"trigger": "failure", "event_id": f"job-{n}"}, snapshot_id="live")


def make(tmp_path, events, **kw):
    store = AdmissionStore(tmp_path / "adm.sqlite")
    w = Watcher("failures", "failure", FeedSource(events))
    return store, Scheduler(store, [w], **kw)


def test_fires_once_per_new_event_and_ignores_duplicates(tmp_path) -> None:
    store, sch = make(tmp_path, [ev(1), ev(2)])
    r1 = sch.tick()
    assert len(r1.admitted) == 2
    assert sch.tick().admitted == []  # cursor advanced: nothing new
    dup = store.admit("job-1#1", {"trigger": "failure", "event_id": "job-1"}, "live", StartedBy(StartedByKind.WATCHER))
    assert not dup.created and dup.run_id == r1.admitted[0]


def test_rerun_attempt_is_a_new_event(tmp_path) -> None:
    store, sch = make(tmp_path, [ev(1, 1), ObservedEvent("job-1#2", "2", {"trigger": "failure", "event_id": "job-1"}, "live")])
    assert len(sch.tick().admitted) == 2


def test_paused_watcher_does_not_fire_but_run_once_does(tmp_path) -> None:
    store, sch = make(tmp_path, [ev(1)])
    sch.pause("failures")
    assert sch.tick().admitted == [] and "failures" in sch.tick().skipped
    assert len(sch.tick(only="failures", run_once=True).admitted) == 1


def test_offline_pauses_external_watchers_with_reason(tmp_path) -> None:
    store, sch = make(tmp_path, [ev(1)], mode=lambda: Mode.OFFLINE)
    r = sch.tick()
    assert r.admitted == [] and "offline" in r.skipped["failures"]
    assert "offline" in sch.state("failures")["paused_reason"]


def test_spend_or_containment_holds_then_dispatches_once(tmp_path) -> None:
    reason = {"v": "spend ceiling reached"}
    store, sch = make(tmp_path, [ev(1)], hold_reason=lambda: reason["v"])
    r = sch.tick()
    assert r.held and store.get(r.held[0]).status == HELD
    started = []
    d = Dispatcher(store, lambda a: started.append(a.run_id), work_allowed=lambda: reason["v"])
    assert d.dispatch_pending() == [] and started == []
    assert sch.tick().admitted == [] and sch.tick().duplicates == []  # not re-admitted on the next poll
    reason["v"] = None
    assert d.dispatch_pending() == r.held
    assert d.dispatch_pending() == []  # exactly once
    assert started == r.held


def test_restart_after_processing_does_not_double_fire(tmp_path) -> None:
    store, sch = make(tmp_path, [ev(1)])
    sch.tick()
    store.close()
    store2 = AdmissionStore(tmp_path / "adm.sqlite")
    sch2 = Scheduler(store2, [Watcher("failures", "failure", FeedSource([ev(1)]))])
    assert sch2.tick().admitted == []


class Crash(Exception):
    pass


def test_crash_after_admission_before_cursor_write_yields_one_run(tmp_path) -> None:
    store, sch = make(tmp_path, [ev(1)])
    original = store.set_cursor

    def boom(*a, **k):
        raise Crash()

    store.set_cursor = boom
    with pytest.raises(Crash):
        sch._poll(sch.watchers["failures"], __import__("atlas.scheduler", fromlist=["SchedulerResult"]).SchedulerResult())
    store.set_cursor = original
    r = sch.tick()  # restart: same event polled again
    assert r.admitted == [] and len(r.duplicates) == 1
    assert len(store.by_status(ADMITTED)) == 1


def test_crash_before_admission_commit_loses_nothing(tmp_path) -> None:
    store, sch = make(tmp_path, [ev(1)])
    original = store.admit

    def boom(*a, **k):
        raise Crash()

    store.admit = boom
    r = sch.tick()
    assert "error" in r.skipped["failures"] and store.cursor("failures") is None
    store.admit = original
    assert len(sch.tick().admitted) == 1


def test_crash_during_dispatch_resumes_once(tmp_path) -> None:
    store, sch = make(tmp_path, [ev(1)])
    run_id = sch.tick().admitted[0]
    calls = []

    def start(a):
        calls.append(a.run_id)
        if len(calls) == 1:
            raise Crash()

    d = Dispatcher(store, start)
    with pytest.raises(Crash):
        d.dispatch_pending()
    assert store.get(run_id).status == "dispatching"
    d.dispatch_pending()  # resume after restart
    assert store.get(run_id).status == DISPATCHED and calls == [run_id, run_id]
    assert d.dispatch_pending() == []


def test_concurrent_pollers_create_one_run(tmp_path) -> None:
    store = AdmissionStore(tmp_path / "adm.sqlite")
    results = []

    def admit():
        results.append(store.admit("job-7#1", {"trigger": "failure", "event_id": "job-7"}, "live", StartedBy(StartedByKind.WATCHER)))

    threads = [threading.Thread(target=admit) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len({r.run_id for r in results}) == 1 and sum(r.created for r in results) == 1


def test_manual_and_automatic_admissions_differ_only_in_started_by(tmp_path) -> None:
    store = AdmissionStore(tmp_path / "adm.sqlite")
    trig = {"trigger": "failure", "event_id": "job-9"}
    auto = store.admit("job-9#1", trig, "live", StartedBy(StartedByKind.WATCHER, watcher_id="failures"))
    man = manual_admission(store, "key-123", trig, "live", StartedBy(StartedByKind.PERSON, role=Role.EM))
    assert auto.trigger == man.trigger and auto.snapshot_id == man.snapshot_id
    assert auto.started_by["kind"] == "watcher" and man.started_by == {"kind": "person", "role": "em", "watcher_id": None}
    again = manual_admission(store, "key-123", trig, "live", StartedBy(StartedByKind.PERSON, role=Role.EM))
    assert not again.created and again.run_id == man.run_id


def test_nightly_due_once_per_day() -> None:
    now = datetime(2026, 11, 14, 2, 30, tzinfo=timezone.utc)
    assert nightly_due(now, "02:00", None)
    assert not nightly_due(now, "03:00", None)
    assert not nightly_due(now, "02:00", now.isoformat())
