"""The automatic runs of a running server.

``register_watchers`` puts the real watchers and schedules into the scheduler, so the API can show
them, turn them on and off, and run them once:

* **Release watcher** (per project). Sees a new latest release, freezes the project's live data and
  admits a release-close run on that copy.
* **Failure watcher** (per project). Sees a newly reported bug or flaky-test issue and admits an
  investigation of it.
* **Nightly sweep** (per project). Once a day, admits one sweep over the open issues that have not
  been investigated yet (at most ten; the rest are carried over).
* **Live dashboard** (per project). Polls the project's public sources for the dashboard.
* **Housekeeping**. Expires approvals, access grants and sessions.

The first three start runs that use the model. They ship **paused**: nothing spends until a person
turns them on. They read what the dashboard poller has already stored, so they make no network
calls of their own.

``SchedulerLoop`` ticks the scheduler in one background thread and dispatches what was admitted.
"""

from __future__ import annotations

import re
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping

from atlas.contracts import Mode
from atlas.scheduler import ObservedEvent, Scheduler, Watcher, nightly_due

SPENDS = "Off until a person turns it on: it starts runs that use the model."
NIGHTLY_LIMIT = 10
_ISSUE = re.compile(r"#(\d+)")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _name(services: Any, project_id: str) -> str:
    projects = getattr(services, "projects", None)
    cfg = projects.get(project_id) if projects is not None and hasattr(projects, "get") else None
    return getattr(cfg, "name", None) or project_id


def _panel(services: Any, project_id: str, panel_id: str) -> list[Mapping[str, Any]]:
    board = services.feed.dashboard(project_id)
    for panel in board.get("panels", []):
        if panel.get("panel_id") == panel_id:
            return list(panel.get("items", []))
    return []


def _freeze(services: Any, project_id: str) -> str:
    return str(services.snapshots.freeze(project_id)["snapshot_id"])


def _investigated(services: Any, project_id: str) -> set[str]:
    """Issue numbers this project already has an investigation for."""
    done = set()
    for adm in services.admissions.for_project(project_id):
        trigger = adm.trigger or {}
        if trigger.get("trigger") == "failure" and trigger.get("event_id") is not None:
            done.add(str(trigger["event_id"]))
        for queued in trigger.get("queued") or []:
            done.add(str(queued))
    return done


def release_watcher(services: Any, project_id: str, interval: float) -> Watcher:
    state_key = f"{project_id}\x1frelease:{project_id}"

    def poll(cursor: str | None) -> Iterable[ObservedEvent]:
        items = _panel(services, project_id, "latest_release")
        tag = str(items[0].get("tag") or "") if items else ""
        if not tag or tag == cursor:
            return []
        if cursor is None:  # the first look only notes the current release; it is not a new one
            services.admissions.set_cursor(state_key, tag)
            return []
        snapshot_id = _freeze(services, project_id)
        return [ObservedEvent(key=f"release:{tag}", position=tag, snapshot_id=snapshot_id,
                              trigger={"trigger": "release_close", "release_id": tag, "snapshot_as_of": None})]

    return Watcher(f"release:{project_id}", "release", poll, external=True, default_enabled=False,
                   project_id=project_id, title=f"Release close when {_name(services, project_id)} ships a release",
                   interval_seconds=interval, spends_model_budget=True, default_paused_reason=SPENDS)


def failure_watcher(services: Any, project_id: str, interval: float) -> Watcher:
    state_key = f"{project_id}\x1ffailure:{project_id}"

    def numbers() -> list[int]:
        found = set()
        for item in _panel(services, project_id, "open_issues"):
            if str(item.get("number") or "").isdigit():
                found.add(int(item["number"]))
        for entry in services.feed.activity(project_id):
            match = _ISSUE.match(str(entry.get("title", "")))
            if entry.get("kind") == "issue" and match:
                found.add(int(match.group(1)))
        return sorted(found)

    def poll(cursor: str | None) -> Iterable[ObservedEvent]:
        seen = numbers()
        if not seen:
            return []
        if cursor is None:  # the first look only notes what is already open
            services.admissions.set_cursor(state_key, str(seen[-1]))
            return []
        fresh = [n for n in seen if n > int(cursor)]
        if not fresh:
            return []
        snapshot_id = _freeze(services, project_id)
        return [ObservedEvent(key=f"issue:{n}", position=str(n), snapshot_id=snapshot_id,
                              trigger={"trigger": "failure", "event_id": str(n),
                                       "summary": f"failure reported in issue {n}", "components": []})
                for n in fresh]

    return Watcher(f"failure:{project_id}", "failure", poll, external=True, default_enabled=False,
                   project_id=project_id, title=f"Investigate new failures in {_name(services, project_id)}",
                   interval_seconds=interval, spends_model_budget=True, default_paused_reason=SPENDS)


def nightly_watcher(services: Any, project_id: str, nightly_time: str,
                    clock: Callable[[], datetime] = _now) -> Watcher:
    wid = f"nightly:{project_id}"

    def poll(cursor: str | None) -> Iterable[ObservedEvent]:
        now = clock()
        today = now.date().isoformat()
        if cursor == today or not nightly_due(now, nightly_time, None):
            return []
        done = _investigated(services, project_id)
        waiting = [str(i["number"]) for i in _panel(services, project_id, "open_issues")
                   if i.get("number") is not None and str(i["number"]) not in done]
        if not waiting:
            return []
        queued, carried = waiting[:NIGHTLY_LIMIT], len(waiting[NIGHTLY_LIMIT:])
        return [ObservedEvent(key=f"nightly:{today}", position=today, snapshot_id=_freeze(services, project_id),
                              trigger={"trigger": "nightly", "queued": queued, "carried_over": carried})]

    return Watcher(wid, "nightly", poll, external=False, default_enabled=False, project_id=project_id,
                   title=f"Nightly sweep of open failures in {_name(services, project_id)} at {nightly_time}",
                   interval_seconds=300.0, spends_model_budget=True, default_paused_reason=SPENDS)


def dashboard_watcher(services: Any, project_id: str, poller: Any, interval: float, enabled: bool) -> Watcher:
    def poll(cursor: str | None) -> Iterable[ObservedEvent]:
        poller.poll_once()
        return []

    return Watcher(f"dashboard:{project_id}", "dashboard", poll, external=True, default_enabled=enabled,
                   project_id=project_id, title=f"Live dashboard for {_name(services, project_id)}",
                   interval_seconds=interval,
                   default_paused_reason=None if enabled else "Turned off in the settings (ATLAS_DASHBOARD_POLL).")


def housekeeping_watcher(run_jobs: Callable[[], Any], interval: float) -> Watcher:
    def poll(cursor: str | None) -> Iterable[ObservedEvent]:
        run_jobs()
        return []

    return Watcher("housekeeping", "housekeeping", poll, external=False, default_enabled=True,
                   title="Housekeeping: expire approvals, access grants and sessions", interval_seconds=interval)


def register_watchers(services: Any, *, env: Mapping[str, str] | None = None, housekeeper: Any = None,
                      nightly_time: str = "02:00", clock: Callable[[], datetime] = _now) -> list[Watcher]:
    """Add every automatic run of this installation to the scheduler. Safe to call once at start."""
    env = dict(env or {})
    scheduler: Scheduler = services.scheduler
    wiring = getattr(services, "wiring", None)
    data = (getattr(wiring, "extra", {}) or {}).get("data") if wiring is not None else None
    pollers = dict(getattr(data, "pollers", {}) or {})
    poll_on = (env.get("ATLAS_DASHBOARD_POLL") or "on").lower() not in ("off", "false", "0")
    poll_seconds = float(getattr(getattr(data, "poll_loop", None), "interval", 60) or 60)
    watchers: list[Watcher] = []
    for project_id, poller in pollers.items():
        watchers.append(dashboard_watcher(services, project_id, poller, poll_seconds, poll_on))
        watchers.append(release_watcher(services, project_id, poll_seconds))
        watchers.append(failure_watcher(services, project_id, poll_seconds))
        watchers.append(nightly_watcher(services, project_id, nightly_time, clock))
    if housekeeper is not None:
        watchers.append(housekeeping_watcher(housekeeper.run_once, housekeeper.interval))
    for w in watchers:
        scheduler.add(w)
    if getattr(services, "feed", None) is not None and pollers:
        services.feed.interval_seconds = int(poll_seconds)
    return watchers


class SchedulerLoop:
    """Ticks the scheduler and dispatches admitted runs in one background thread."""

    def __init__(self, services: Any, tick_seconds: float = 15.0) -> None:
        self.services = services
        self.tick_seconds = max(1.0, float(tick_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> Any:
        result = self.services.scheduler.tick()
        if result.admitted or result.held:
            self.services.dispatcher.dispatch_pending()
        return result

    def start(self) -> None:
        if self._thread is not None:
            return

        def loop() -> None:
            while not self._stop.is_set():
                try:
                    self.run_once()
                except Exception:  # the loop must outlive any one failure
                    pass
                self._stop.wait(self.tick_seconds)

        self._thread = threading.Thread(target=loop, name="atlas-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


__all__ = ["SchedulerLoop", "register_watchers", "Mode"]
