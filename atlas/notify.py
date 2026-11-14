"""Notifications and error flow: no error ends without a notification record.

Severity, kind and roles come only from the event catalog in ``schema/notification_events.json``.
Every event is written durably before any delivery attempt. In-app content is projected per target
role. External destinations declare an audience principal and receive content projected for that
audience; a destination with no declared audience receives nothing. In Offline mode, or while
containment forbids external calls, external deliveries are stored as paused and resume later.
Failed deliveries are retried with back-off, then dead-lettered with an in-app notice; a failure
notice is never sent through the channel that failed.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from atlas.contracts import EgressPurpose, Mode, Principal

DEFAULT_CATALOG = Path(__file__).resolve().parents[1] / "schema" / "notification_events.json"


class CatalogError(ValueError):
    pass


def load_catalog(path: Path = DEFAULT_CATALOG) -> dict[str, dict[str, Any]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    events = data.get("events")
    if not isinstance(events, dict) or not events:
        raise CatalogError("catalog has no events")
    severities = set(data.get("severities", ["info", "warning", "high", "critical"]))
    kinds = set(data.get("kinds", ["alert", "reminder", "decision"]))
    for name, e in events.items():
        if e.get("severity") not in severities or e.get("kind") not in kinds or not e.get("roles"):
            raise CatalogError(f"catalog entry {name} is invalid")
    return events


@dataclass(frozen=True)
class Destination:
    name: str
    url: str
    audience: str | None  # a role, "public", or None when undeclared


SCHEMA = """
CREATE TABLE IF NOT EXISTS notifications (
  id TEXT PRIMARY KEY, event_type TEXT, subject_id TEXT, severity TEXT, kind TEXT, roles TEXT,
  backup_roles TEXT, payload_refs TEXT, content TEXT, required_scopes TEXT, created TEXT,
  status TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS deliveries (
  id TEXT PRIMARY KEY, notification_id TEXT, destination TEXT, status TEXT, reason TEXT,
  attempts INTEGER DEFAULT 0, updated TEXT);
CREATE TABLE IF NOT EXISTS audit_log (at TEXT, entry TEXT);
"""


class NotificationService:
    def __init__(
        self,
        *,
        projection: Any,
        principal_for: Callable[[str], Principal],
        outbound: Any = None,
        destinations: Sequence[Destination] = (),
        mode: Callable[[], Mode] = lambda: Mode.CONNECTED,
        external_allowed: Callable[[], bool] = lambda: True,
        catalog_path: Path = DEFAULT_CATALOG,
        db_path: str | Path = ":memory:",
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        dedup_window: timedelta = timedelta(minutes=10),
        rate_limit_per_role: int = 20,
        rate_window: timedelta = timedelta(minutes=10),
        max_attempts: int = 3,
        backoff: Callable[[int], None] = lambda attempt: time.sleep(0),
    ) -> None:
        self.catalog = load_catalog(catalog_path)
        self.projection = projection
        self.principal_for = principal_for
        self.outbound = outbound
        self.destinations = list(destinations)
        self.mode = mode
        self.external_allowed = external_allowed
        self.clock = clock
        self.dedup_window = dedup_window
        self.rate_limit = rate_limit_per_role
        self.rate_window = rate_window
        self.max_attempts = max_attempts
        self.backoff = backoff
        self.degraded = False
        self._db = sqlite3.connect(str(db_path), isolation_level=None, check_same_thread=False)
        self._db.executescript(SCHEMA)
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ emit

    def emit(self, event_type: str, subject_id: str, payload_refs: Sequence[str],
             required_scopes: frozenset[str], content: Mapping[str, Any] | None = None) -> str:
        """Record a notification. Severity, kind and roles come only from the catalog."""
        if event_type not in self.catalog:
            raise KeyError(f"event type not in catalog: {event_type}")
        entry = self.catalog[event_type]
        now = self.clock()
        with self._lock:
            dup = self._db.execute(
                "SELECT id, created FROM notifications WHERE event_type = ? AND subject_id = ? AND severity = ? "
                "ORDER BY created DESC LIMIT 1", (event_type, subject_id, entry["severity"])
            ).fetchone()
            if dup and now - datetime.fromisoformat(dup[1]) < self.dedup_window:
                return dup[0]
            nid = f"ntf-{uuid.uuid4().hex[:10]}"
            body = dict(content or {"text": event_type.replace(".", " ").replace("_", " ")})
            body.setdefault("required_scopes", sorted(required_scopes))
            self._db.execute(
                "INSERT INTO notifications (id, event_type, subject_id, severity, kind, roles, backup_roles, "
                "payload_refs, content, required_scopes, created) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (nid, event_type, subject_id, entry["severity"], entry["kind"], json.dumps(entry["roles"]),
                 json.dumps(entry.get("backup_roles", [])), json.dumps(list(payload_refs)),
                 json.dumps(body, default=str), json.dumps(sorted(required_scopes)), now.isoformat()),
            )
        if not event_type.startswith(("delivery.", "notifier.")):
            try:
                self._deliver_external(nid)
            except Exception as exc:  # the record already exists; note the fault and continue
                self._fault(exc)
        return nid

    # ------------------------------------------------------------- in-app

    def _row(self, r: tuple) -> dict[str, Any]:
        keys = ("id", "event_type", "subject_id", "severity", "kind", "roles", "backup_roles", "payload_refs",
                "content", "required_scopes", "created", "status")
        d = dict(zip(keys, r))
        for k in ("roles", "backup_roles", "payload_refs", "content", "required_scopes"):
            d[k] = json.loads(d[k])
        return d

    def records(self) -> list[dict[str, Any]]:
        return [self._row(r) for r in self._db.execute("SELECT * FROM notifications ORDER BY created").fetchall()]

    def in_app(self, principal: Principal) -> list[dict[str, Any]]:
        """Notifications for the principal's role, with restricted parts withheld."""
        role = str(principal.role) if principal.role else None
        out = []
        recent = self.clock() - self.rate_window
        count = 0
        for n in self.records():
            if role not in n["roles"] and role not in n["backup_roles"]:
                continue
            if n["severity"] != "critical" and datetime.fromisoformat(n["created"]) >= recent:
                count += 1
                if count > self.rate_limit:
                    continue
            projected = self.projection.project({"required_scopes": [], "content": n["content"]}, principal)
            out.append({**{k: n[k] for k in ("id", "event_type", "subject_id", "severity", "kind", "created", "status")},
                        "content": projected.get("content"), "restricted_items": projected.get("restricted_items", 0)})
        return out

    def acknowledge(self, nid: str) -> None:
        self._db.execute("UPDATE notifications SET status = 'acknowledged' WHERE id = ?", (nid,))

    # ------------------------------------------------------------ external

    def _set_delivery(self, nid: str, dest: str, status: str, reason: str | None, attempts: int = 0) -> None:
        with self._lock:
            row = self._db.execute("SELECT id FROM deliveries WHERE notification_id = ? AND destination = ?", (nid, dest)).fetchone()
            now = self.clock().isoformat()
            if row:
                self._db.execute("UPDATE deliveries SET status = ?, reason = ?, attempts = attempts + ?, updated = ? WHERE id = ?",
                                 (status, reason, attempts, now, row[0]))
            else:
                self._db.execute("INSERT INTO deliveries VALUES (?,?,?,?,?,?,?)",
                                 (f"dlv-{uuid.uuid4().hex[:10]}", nid, dest, status, reason, attempts, now))

    def deliveries(self, status: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM deliveries" + (" WHERE status = ?" if status else "")
        rows = self._db.execute(q, (status,) if status else ()).fetchall()
        return [dict(zip(("id", "notification_id", "destination", "status", "reason", "attempts", "updated"), r)) for r in rows]

    def _deliver_external(self, nid: str) -> None:
        n = next(r for r in self.records() if r["id"] == nid)
        for dest in self.destinations:
            if dest.audience is None:
                self._set_delivery(nid, dest.name, "held", "audience_undeclared")
                self.emit("delivery.audience_undeclared", dest.name, [nid], frozenset({"internal"}),
                          {"text": f"destination {dest.name} needs a declared audience", "required_scopes": ["internal"]})
                continue
            if self.mode() == Mode.OFFLINE or not self.external_allowed():
                self._set_delivery(nid, dest.name, "paused", "offline" if self.mode() == Mode.OFFLINE else "containment")
                continue
            self._attempt(n, dest)

    def _attempt(self, n: Mapping[str, Any], dest: Destination) -> None:
        principal = self.principal_for(dest.audience)
        projected = self.projection.project({"required_scopes": [], "content": n["content"]}, principal)
        body = {"event_type": n["event_type"], "severity": n["severity"], "subject_id": n["subject_id"],
                "content": projected.get("content"), "restricted_items": projected.get("restricted_items", 0)}
        last_error = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                self.outbound.call("webhook", EgressPurpose.WEBHOOK_DELIVERY, {"url": dest.url, "body": body})
                self._set_delivery(n["id"], dest.name, "delivered", None, attempt)
                return
            except Exception as exc:
                last_error = type(exc).__name__
                self.backoff(attempt)
        self._set_delivery(n["id"], dest.name, "dead_letter", last_error, self.max_attempts)
        # In-app only: never through the channel that failed, never through any external channel.
        self.emit("delivery.dead_lettered", n["id"], [n["id"]], frozenset({"internal"}),
                  {"text": f"delivery to {dest.name} failed", "required_scopes": ["internal"]})

    def resume_paused(self) -> int:
        """Retry paused deliveries once the mode and containment allow it."""
        if self.mode() == Mode.OFFLINE or not self.external_allowed():
            return 0
        paused = self.deliveries("paused")
        dests = {d.name: d for d in self.destinations}
        for p in paused:
            n = next(r for r in self.records() if r["id"] == p["notification_id"])
            if p["destination"] in dests:
                self._attempt(n, dests[p["destination"]])
        return len(paused)

    # ---------------------------------------------------------------- faults

    def _fault(self, exc: Exception) -> None:
        self.degraded = True
        self._db.execute("INSERT INTO audit_log VALUES (?, ?)", (self.clock().isoformat(), f"notifier fault: {type(exc).__name__}"))

    def audit_log(self) -> list[str]:
        return [r[1] for r in self._db.execute("SELECT * FROM audit_log").fetchall()]

    def health(self) -> dict[str, Any]:
        return {"notifier": "degraded" if self.degraded else "ok",
                "dead_letters": len(self.deliveries("dead_letter")), "paused": len(self.deliveries("paused"))}
