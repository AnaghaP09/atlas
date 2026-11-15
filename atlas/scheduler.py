"""Scheduler, watchers and transactional run admission.

Watchers never start runs themselves. Each observed event goes through one admission transaction
that records the unique event key, the bound snapshot, the run id and a dispatch intent. Dispatch
runs from that durable intent, is idempotent per run id and resumes after a crash. A watcher's
cursor advances only after admission has committed, so a crash can neither lose an event nor start
it twice. Events admitted while the spend ceiling or containment forbids work are kept as ``held``
and dispatched once when released.

Admissions, cursors and watcher state are partitioned by project: the same source event watched
by two projects is two admissions (two runs), and each project's watcher keeps its own cursor.
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
from atlas.sqlite_util import connect as safe_connect

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

COLUMNS = "event_key, run_id, snapshot_id, trigger, started_by, status, hold_reason, admitted_at, project_id"

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
    project_id: str = ""
    admitted_at: str | None = None


_SEP = "\x1f"


def _stored_key(project_id: str, event_key: str) -> str:
    """The stored unique key: the project plus the event key."""
    return f"{project_id}{_SEP}{event_key}" if project_id else event_key


def _split_key(stored: str) -> tuple[str, str]:
    if _SEP in stored:
        project_id, event_key = stored.split(_SEP, 1)
        return project_id, event_key
    return "", stored


class AdmissionStore:
    """The single entry point for every run, automatic or manual."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._db = safe_connect(str(path), isolation_level=None, check_same_thread=False)
        self._db.executescript(SCHEMA)
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(admissions)").fetchall()}
        if "project_id" not in cols:
            self._db.execute("ALTER TABLE admissions ADD COLUMN project_id TEXT NOT NULL DEFAULT ''")
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
        project_id: str = "",
    ) -> Admission:
        """Record the event, its run and the dispatch intent in one transaction.

        A repeated event key in the same project (duplicate delivery or a racing poller) returns the
        existing run; the same event key in another project is a separate admission.
        """
        stored = _stored_key(project_id, event_key)
        sb = {"kind": str(started_by.kind), "role": str(started_by.role) if started_by.role else None,
              "watcher_id": started_by.watcher_id}
        with self._lock:
            cur = self._db.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                row = cur.execute(f"SELECT {COLUMNS} FROM admissions WHERE event_key = ?", (stored,)).fetchone()
                if row:
                    cur.execute("COMMIT")
                    return self._row(row, created=False)
                run_id = f"run-{uuid.uuid4().hex[:12]}"
                status = HELD if hold_reason else ADMITTED
                cur.execute(
                    f"INSERT INTO admissions ({COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?)",
                    (stored, run_id, snapshot_id, json.dumps(dict(trigger), sort_keys=True, default=str),
                     json.dumps(sb), status, hold_reason, (now or datetime.now(timezone.utc)).isoformat(), project_id),
                )
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise
        return Admission(event_key, run_id, snapshot_id, dict(trigger), sb, status, True, hold_reason, project_id)

    @staticmethod
    def _row(row: tuple, created: bool) -> Admission:
        _, event_key = _split_key(row[0])
        return Admission(
            event_key=event_key, run_id=row[1], snapshot_id=row[2], trigger=json.loads(row[3]),
            started_by=json.loads(row[4]), status=row[5], created=created, hold_reason=row[6],
            project_id=row[8] or "", admitted_at=row[7],
        )

    def get(self, run_id: str) -> Admission | None:
        row = self._db.execute(f"SELECT {COLUMNS} FROM admissions WHERE run_id = ?", (run_id,)).fetchone()
        return self._row(row, created=False) if row else None

    def for_project(self, project_id: str) -> list[Admission]:
        rows = self._db.execute(f"SELECT {COLUMNS} FROM admissions WHERE project_id = ? ORDER BY admitted_at",
                                (project_id,)).fetchall()
        return [self._row(r, created=False) for r in rows]

    def by_status(self, *statuses: str) -> list[Admission]:
        marks = ",".join("?" * len(statuses))
        rows = self._db.execute(
            f"SELECT {COLUMNS} FROM admissions WHERE status IN ({marks}) ORDER BY admitted_at", statuses
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
    project_id: str = ""
    title: str = ""
    # Seconds between polls; 0 polls on every scheduler tick.
    interval_seconds: float = 0.0
    # True when this watcher starts runs that use the model. Such watchers ship paused.
    spends_model_budget: bool = False
    # Shown while the watcher has never been turned on by a person.
    default_paused_reason: str | None = None

    @property
    def state_key(self) -> str:
        """Cursor and state key: a project's watcher never shares a cursor with another project's."""
        return f"{self.project_id}{_SEP}{self.watcher_id}" if self.project_id else self.watcher_id


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
        st = {**self.store.watcher_state(w.state_key, w.default_enabled), "watcher_id": watcher_id,
              "project_id": w.project_id or None}
        if not st["enabled"] and not st.get("paused_reason") and w.default_paused_reason:
            st = {**st, "paused_reason": w.default_paused_reason}
        if w.external and self.mode() == Mode.OFFLINE:
            st = {**st, "paused_reason": "offline: external sources are not reachable"}
        st["next_run"] = self.next_run(watcher_id, st)
        return st

    def next_run(self, watcher_id: str, st: Mapping[str, Any] | None = None) -> str | None:
        """When an enabled watcher polls next (None while paused, offline or never timed)."""
        w = self.watchers[watcher_id]
        st = st or self.store.watcher_state(w.state_key, w.default_enabled)
        if not st["enabled"] or (w.external and self.mode() == Mode.OFFLINE) or not w.interval_seconds:
            return None
        if not st.get("last_run"):
            return self.clock().isoformat()
        from datetime import timedelta

        return (datetime.fromisoformat(st["last_run"]) + timedelta(seconds=w.interval_seconds)).isoformat()

    def due(self, watcher_id: str) -> bool:
        w = self.watchers[watcher_id]
        if not w.interval_seconds:
            return True
        last = self.store.watcher_state(w.state_key, w.default_enabled).get("last_run")
        return last is None or (self.clock() - datetime.fromisoformat(last)).total_seconds() >= w.interval_seconds

    def add(self, watcher: Watcher) -> None:
        self.watchers[watcher.watcher_id] = watcher

    def enable(self, watcher_id: str) -> None:
        w = self.watchers[watcher_id]
        st = self.store.watcher_state(w.state_key, w.default_enabled)
        self.store.save_watcher({**st, "enabled": True, "paused_reason": None})

    def pause(self, watcher_id: str, reason: str = "paused by a person") -> None:
        w = self.watchers[watcher_id]
        st = self.store.watcher_state(w.state_key, w.default_enabled)
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
            if not run_once and not self.due(wid):
                continue
            try:
                self._poll(watcher, result, started_by)
            except Exception as exc:  # a failing watcher must not stop the others
                result.skipped[wid] = f"error: {type(exc).__name__}"
                if self.on_error:
                    self.on_error(wid, exc)
        return result

    def _poll(self, watcher: Watcher, result: SchedulerResult, origin: StartedBy | None = None) -> None:
        cursor = self.store.cursor(watcher.state_key)
        last_event = None
        for ev in watcher.poll(cursor):
            hold = self.hold_reason()
            started_by = origin or StartedBy(
                kind=StartedByKind.SCHEDULE if watcher.kind in {"nightly", "digest"} else StartedByKind.WATCHER,
                watcher_id=watcher.watcher_id,
            )
            adm = self.store.admit(ev.key, ev.trigger, ev.snapshot_id, started_by, hold_reason=hold, now=self.clock(),
                                   project_id=watcher.project_id)
            if not adm.created:
                result.duplicates.append(adm.run_id)
            elif adm.status == HELD:
                result.held.append(adm.run_id)
            else:
                result.admitted.append(adm.run_id)
            # The cursor moves only after the admission above has committed.
            self.store.set_cursor(watcher.state_key, ev.position)
            last_event = ev.key
        st = self.store.watcher_state(watcher.state_key, watcher.default_enabled)
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
                     snapshot_id: str, started_by: StartedBy, hold_reason: str | None = None,
                     project_id: str = "") -> Admission:
    """Manual triggers use the same admission; the client's idempotency key is the event key."""
    return store.admit(f"manual:{idempotency_key}", trigger, snapshot_id, started_by, hold_reason=hold_reason,
                       project_id=project_id)
