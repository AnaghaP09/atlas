"""The live project dashboard: panels, activity feed and the project event stream.

Watchers poll each project's sources and hand what changed to this feed. The feed keeps a small
dashboard per project (panels with a version) and an activity list, and publishes two events on
``/api/projects/{project_id}/stream``:

* ``project.activity``: one per new or changed record;
* ``project.dashboard_changed``: the dashboard version and the ids of the panels that changed, so
  the UI refetches only those panels.

Ingestion never starts an investigation. Until the data layer is connected the panels say so and
the activity list is empty; nothing is invented.
"""

from __future__ import annotations

import threading
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from atlas.api.streams import PROJECT_STREAM

PANELS: tuple[tuple[str, str], ...] = (
    ("open_issues", "Open bug and flaky-test issues"),
    ("failing_ci", "Failing CI jobs"),
    ("latest_release", "Latest release"),
    ("security", "Security findings"),
)
NOT_CONNECTED = "the data layer is not connected in this build yet"
ACTIVITY_LIMIT = 500


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ProjectFeed:
    def __init__(self, hub: Any, *, interval_seconds: int = 60, clock: Callable[[], datetime] = _now) -> None:
        self.hub = hub
        self.interval_seconds = interval_seconds
        self.clock = clock
        self._lock = threading.Lock()
        self._dash: dict[str, dict[str, Any]] = {}
        self._activity: dict[str, deque[dict[str, Any]]] = {}
        self.connected = False  # set by the data layer when it starts feeding

    # ------------------------------------------------------------------ state

    def _board(self, project_id: str) -> dict[str, Any]:
        board = self._dash.get(project_id)
        if board is None:
            board = {"dashboard_version": 0, "last_poll": None, "history": [],
                     "panels": {pid: {"panel_id": pid, "title": title, "status": "not_connected",
                                      "items": [], "updated_at": None} for pid, title in PANELS}}
            self._dash[project_id] = board
        return board

    # ------------------------------------------------------------------ writes (data layer)

    def record_poll(self, project_id: str, at: datetime | None = None) -> None:
        with self._lock:
            self._board(project_id)["last_poll"] = (at or self.clock()).isoformat()

    def set_history(self, project_id: str, points: Sequence[Mapping[str, Any]]) -> None:
        """Counts per poll (oldest first), kept by the data layer across restarts."""
        with self._lock:
            self._board(project_id)["history"] = [dict(p) for p in points]

    def update_panels(self, project_id: str, panels: Mapping[str, Sequence[Mapping[str, Any]]],
                      totals: Mapping[str, int] | None = None) -> int:
        """Replace the items of the named panels and announce one dashboard change.

        ``totals`` gives the full count behind a panel that shows only its newest rows."""
        now = self.clock().isoformat()
        with self._lock:
            board = self._board(project_id)
            changed = []
            for pid, items in panels.items():
                if pid not in board["panels"]:
                    raise KeyError(f"unknown panel {pid}")
                board["panels"][pid].update(status="live", items=[dict(i) for i in items], updated_at=now)
                if totals and pid in totals:
                    board["panels"][pid]["total"] = int(totals[pid])
                changed.append(pid)
            board["dashboard_version"] += 1
            version = board["dashboard_version"]
        self._publish("project.dashboard_changed", project_id,
                      {"project_id": project_id, "dashboard_version": version, "changed_panels": changed},
                      ["internal"])
        return version

    def record_activity(self, project_id: str, item: Mapping[str, Any]) -> dict[str, Any]:
        """Add one new or changed record to the activity list and announce it."""
        entry = {
            "activity_id": f"act-{uuid.uuid4().hex[:10]}", "project_id": project_id,
            "kind": str(item.get("kind", "record")), "title": str(item.get("title", "")),
            "record_id": item.get("record_id"), "content_version_id": item.get("content_version_id"),
            "source_revision": item.get("source_revision"), "at": self.clock().isoformat(),
            "required_scopes": list(item.get("required_scopes") or ["internal"]),
        }
        if entry["record_id"]:
            entry["link"] = (f"/api/projects/{project_id}/evidence/{entry['record_id']}"
                             f"?content_version_id={entry['content_version_id'] or ''}")
        with self._lock:
            self._activity.setdefault(project_id, deque(maxlen=ACTIVITY_LIMIT)).appendleft(entry)
        data = {k: entry.get(k) for k in ("project_id", "kind", "title", "record_id", "content_version_id",
                                          "source_revision", "link")}
        self._publish("project.activity", project_id, data, entry["required_scopes"])
        return entry

    def _publish(self, event: str, project_id: str, data: Mapping[str, Any], scopes: Sequence[str]) -> None:
        wire = {"event": event, "event_id": f"evt-{uuid.uuid4().hex[:12]}", "emitted_at": self.clock().isoformat(),
                "run_id": None, "authoritative_generation": None, "report_version": None,
                "correction_version": None, "data": dict(data)}
        self.hub.publish(PROJECT_STREAM, wire, list(scopes), project_id=project_id)

    # ------------------------------------------------------------------ reads (API)

    def dashboard(self, project_id: str) -> dict[str, Any]:
        with self._lock:
            board = self._board(project_id)
            panels = []
            for p in board["panels"].values():
                panel = dict(p, items=[dict(i) for i in p["items"]], project_id=project_id)
                if panel["status"] == "not_connected":
                    panel["reason"] = NOT_CONNECTED
                panels.append(panel)
            return {"project_id": project_id, "dashboard_version": board["dashboard_version"],
                    "last_poll": board["last_poll"], "interval_seconds": self.interval_seconds,
                    "history": [dict(p) for p in board.get("history", [])],
                    "panels": panels, "data_mode": "REAL" if self.connected else "STAGED"}

    def activity(self, project_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(e) for e in list(self._activity.get(project_id, ()))[:max(1, min(limit, ACTIVITY_LIMIT))]]
