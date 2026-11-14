"""Scheduler, watchers and transactional run admission.

Watchers never start runs themselves. Each observed event goes through one admission transaction
that records the unique event key, the bound snapshot, the run id and a dispatch intent. Dispatch
runs from that durable intent, is idempotent per run id and resumes after a crash. A watcher's
cursor advances only after admission has committed, so a crash can neither lose an event nor start
it twice. Events admitted while the spend ceiling or containment forbids work are kept as ``held``
and dispatched once when released.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from atlas.contracts import Mode, StartedBy, StartedByKind

SCHEMA = """
CREATE TABLE IF NOT EXISTS admissions (
    event_key TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE,
    snapshot_id TEXT NOT NULL,
    trigger TEXT NOT NULL,
    started_by TEXT NOT NULL,
    status TEXT NOT NULL,
    hold_reason TEXT,
    admitted_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cursors (
    watcher_id TEXT PRIMARY KEY,
    cursor TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS watchers (
    watcher_id TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL,
    paused_reason TEXT,
    last_run TEXT,
    last_event TEXT
);
"""

# Admission status values.
ADMITTED, HELD, DISPATCHING, DISPATCHED = "admitted", "held", "dispatching", "dispatched"


@dataclass(frozen=True)
class Admission:
    event_key: str
    run_id: str
    snapshot_id: str
    trigger: dict[str, Any]
    started_by: dict[str, Any]
    status: str
    created: bool
    hold_reason: str | None = None


class AdmissionStore:
    """The single entry point for every run, automatic or manual."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()

    def close(self) -> None:
        self._db.close()

    def admit(
        self,
        event_key: str,
        trigger: Mapping[str, Any],
        snapshot_id: str,
        started_by: StartedBy,
        *,
        hold_reason: str | None = None,
        now: datetime | None = None,
    ) -> Admission:
        """Record the event, its run and the dispatch intent in one transaction.

        A repeated event key (duplicate delivery or a racing poller) returns the existing run.
        """
        sb = {"kind": str(started_by.kind), "role": str(started_by.role) if started_by.role else None,
              "watcher_id": started_by.watcher_id}
        with self._lock:
            cur = self._db.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                row = cur.execute("SELECT * FROM admissions WHERE event_key = ?", (event_key,)).fetchone()
                if row:
                    cur.execute("COMMIT")
                    return self._row(row, created=False)
                run_id = f"run-{uuid.uuid4().hex[:12]}"
                status = HELD if hold_reason else ADMITTED
                cur.execute(
                    "INSERT INTO admissions VALUES (?,?,?,?,?,?,?,?)",
                    (event_key, run_id, snapshot_id, json.dumps(dict(trigger), sort_keys=True, default=str),
                     json.dumps(sb), status, hold_reason, (now or datetime.now(timezone.utc)).isoformat()),
                )
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise
        return Admission(event_key, run_id, snapshot_id, dict(trigger), sb, status, True, hold_reason)

    @staticmethod
    def _row(row: tuple, created: bool) -> Admission:
        return Admission(
            event_key=row[0], run_id=row[1], snapshot_id=row[2], trigger=json.loads(row[3]),
            started_by=json.loads(row[4]), status=row[5], created=created, hold_reason=row[6],
        )

    def get(self, run_id: str) -> Admission | None:
        row = self._db.execute("SELECT * FROM admissions WHERE run_id = ?", (run_id,)).fetchone()
        return self._row(row, created=False) if row else None

    def by_status(self, *statuses: str) -> list[Admission]:
        marks = ",".join("?" * len(statuses))
        rows = self._db.execute(
            f"SELECT * FROM admissions WHERE status IN ({marks}) ORDER BY admitted_at", statuses
        ).fetchall()
        return [self._row(r, created=False) for r in rows]

    def set_status(self, run_id: str, status: str, hold_reason: str | None = None) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE admissions SET status = ?, hold_reason = ? WHERE run_id = ?", (status, hold_reason, run_id)
            )

    def release_held(self) -> list[str]:
        """Move held admissions back to admitted (the caller checks that work is allowed)."""
        held = [a.run_id for a in self.by_status(HELD)]
        for run_id in held:
            self.set_status(run_id, ADMITTED)
        return held

    # cursors ---------------------------------------------------------------------

    def cursor(self, watcher_id: str) -> str | None:
        row = self._db.execute("SELECT cursor FROM cursors WHERE watcher_id = ?", (watcher_id,)).fetchone()
        return row[0] if row else None

    def set_cursor(self, watcher_id: str, value: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO cursors VALUES (?, ?) ON CONFLICT(watcher_id) DO UPDATE SET cursor = excluded.cursor",
                (watcher_id, value),
            )

    # watcher state -----------------------------------------------------------------

    def watcher_state(self, watcher_id: str, default_enabled: bool) -> dict[str, Any]:
        row = self._db.execute("SELECT * FROM watchers WHERE watcher_id = ?", (watcher_id,)).fetchone()
        if not row:
            return {"watcher_id": watcher_id, "enabled": default_enabled, "paused_reason": None,
                    "last_run": None, "last_event": None}
        return {"watcher_id": row[0], "enabled": bool(row[1]), "paused_reason": row[2],
                "last_run": row[3], "last_event": row[4]}

    def save_watcher(self, state: Mapping[str, Any]) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO watchers VALUES (?,?,?,?,?) ON CONFLICT(watcher_id) DO UPDATE SET "
                "enabled = excluded.enabled, paused_reason = excluded.paused_reason, "
                "last_run = excluded.last_run, last_event = excluded.last_event",
                (state["watcher_id"], int(state["enabled"]), state.get("paused_reason"),
                 state.get("last_run"), state.get("last_event")),
            )


class Dispatcher:
    """Runs admitted work from the durable intent. ``start_run`` must be idempotent per run id."""

    def __init__(self, store: AdmissionStore, start_run: Callable[[Admission], None],
                 work_allowed: Callable[[], str | None] = lambda: None) -> None:
        self.store = store
        self.start_run = start_run
        self.work_allowed = work_allowed

    def dispatch_pending(self) -> list[str]:
        """Dispatch admitted and interrupted work; held work waits until it is released."""
        reason = self.work_allowed()
        if reason is None:
            self.store.release_held()
        started = []
        for adm in self.store.by_status(DISPATCHING, ADMITTED):
            if reason is not None:
                self.store.set_status(adm.run_id, HELD, reason)
                continue
            self.store.set_status(adm.run_id, DISPATCHING)
            self.start_run(adm)
            self.store.set_status(adm.run_id, DISPATCHED)
            started.append(adm.run_id)
        return started


# ---------------------------------------------------------------------------
# Watchers
# ---------------------------------------------------------------------------


@dataclass
class ObservedEvent:
    """An event a watcher saw. ``key`` must be unique per real-world event (id plus attempt)."""

    key: str
    position: str
    trigger: dict[str, Any]
    snapshot_id: str


@dataclass
class Watcher:
    watcher_id: str
    kind: str  # release | failure | nightly | digest
    poll: Callable[[str | None], Iterable[ObservedEvent]]
    external: bool = True
    default_enabled: bool = True


@dataclass
class SchedulerResult:
    admitted: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    held: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)


class Scheduler:
    """Polls watchers inside the backend process and admits what they observe."""

    def __init__(
        self,
        store: AdmissionStore,
        watchers: Iterable[Watcher],
        *,
        mode: Callable[[], Mode] = lambda: Mode.CONNECTED,
        hold_reason: Callable[[], str | None] = lambda: None,
        on_error: Callable[[str, Exception], None] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.store = store
        self.watchers = {w.watcher_id: w for w in watchers}
        self.mode = mode
        self.hold_reason = hold_reason
        self.on_error = on_error
        self.clock = clock

    def state(self, watcher_id: str) -> dict[str, Any]:
        w = self.watchers[watcher_id]
        st = self.store.watcher_state(watcher_id, w.default_enabled)
        if w.external and self.mode() == Mode.OFFLINE:
            st = {**st, "paused_reason": "offline: external sources are not reachable"}
        return st

    def enable(self, watcher_id: str) -> None:
        st = self.store.watcher_state(watcher_id, self.watchers[watcher_id].default_enabled)
        self.store.save_watcher({**st, "enabled": True, "paused_reason": None})

    def pause(self, watcher_id: str, reason: str = "paused by a person") -> None:
        st = self.store.watcher_state(watcher_id, self.watchers[watcher_id].default_enabled)
        self.store.save_watcher({**st, "enabled": False, "paused_reason": reason})

    def tick(self, only: str | None = None, run_once: bool = False,
             started_by: StartedBy | None = None) -> SchedulerResult:
        result = SchedulerResult()
        for wid, watcher in self.watchers.items():
            if only and wid != only:
                continue
            st = self.state(wid)
            if watcher.external and self.mode() == Mode.OFFLINE:
                result.skipped[wid] = st["paused_reason"]
                continue
            if not st["enabled"] and not run_once:
                result.skipped[wid] = st["paused_reason"] or "disabled"
                continue
            try:
                self._poll(watcher, result, started_by)
            except Exception as exc:  # a failing watcher must not stop the others
                result.skipped[wid] = f"error: {type(exc).__name__}"
                if self.on_error:
                    self.on_error(wid, exc)
        return result

    def _poll(self, watcher: Watcher, result: SchedulerResult, origin: StartedBy | None = None) -> None:
        cursor = self.store.cursor(watcher.watcher_id)
        last_event = None
        for ev in watcher.poll(cursor):
            hold = self.hold_reason()
            started_by = origin or StartedBy(
                kind=StartedByKind.SCHEDULE if watcher.kind in {"nightly", "digest"} else StartedByKind.WATCHER,
                watcher_id=watcher.watcher_id,
            )
            adm = self.store.admit(ev.key, ev.trigger, ev.snapshot_id, started_by, hold_reason=hold, now=self.clock())
            if not adm.created:
                result.duplicates.append(adm.run_id)
            elif adm.status == HELD:
                result.held.append(adm.run_id)
            else:
                result.admitted.append(adm.run_id)
            # The cursor moves only after the admission above has committed.
            self.store.set_cursor(watcher.watcher_id, ev.position)
            last_event = ev.key
        st = self.store.watcher_state(watcher.watcher_id, watcher.default_enabled)
        self.store.save_watcher({**st, "last_run": self.clock().isoformat(), "last_event": last_event or st.get("last_event")})


def nightly_due(now: datetime, nightly_time: str, last_run: str | None) -> bool:
    """True once per day after the configured local time (HH:MM)."""
    hh, mm = (int(x) for x in nightly_time.split(":"))
    if now.time() < time(hh, mm):
        return False
    if last_run is None:
        return True
    return datetime.fromisoformat(last_run).date() < now.date()


def manual_admission(store: AdmissionStore, idempotency_key: str, trigger: Mapping[str, Any],
                     snapshot_id: str, started_by: StartedBy, hold_reason: str | None = None) -> Admission:
    """Manual triggers use the same admission; the client's idempotency key is the event key."""
    return store.admit(f"manual:{idempotency_key}", trigger, snapshot_id, started_by, hold_reason=hold_reason)
