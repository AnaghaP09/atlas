"""Operational database shared by the execution layer.

One SQLite file holds tasks, approvals, the publication journal, containment incidents,
corrections, scorecard rows and an event outbox. Every state change and the event that announces
it are written in the same transaction, so a crash can never leave one without the other.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Mapping

from atlas.contracts import EVENT_NAMES

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    action_id TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    action TEXT NOT NULL,
    task_class TEXT NOT NULL,
    task_text TEXT NOT NULL,
    scope_json TEXT NOT NULL,
    destination_repo TEXT NOT NULL,
    destination_node_id TEXT NOT NULL,
    base_ref TEXT NOT NULL,
    base_commit TEXT NOT NULL,
    publication_ref TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    state TEXT NOT NULL,
    support_status TEXT,
    security_decision_id TEXT,
    override_json TEXT,
    artefact_json TEXT,
    checks_json TEXT NOT NULL DEFAULT '{}',
    published_commit TEXT,
    pr_number INTEGER,
    published_at TEXT,
    defect_window_ends TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_history (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    reason TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    generation INTEGER NOT NULL,
    bindings_json TEXT NOT NULL,
    approver_role TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    voided_at TEXT,
    void_reason TEXT
);
CREATE TABLE IF NOT EXISTS journal (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id TEXT NOT NULL,
    step TEXT NOT NULL,
    status TEXT NOT NULL,
    remote_id TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}',
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS incidents (
    incident_id TEXT PRIMARY KEY,
    row INTEGER NOT NULL,
    trigger TEXT NOT NULL,
    producer TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    publication_id TEXT,
    offending_item_id TEXT,
    status TEXT NOT NULL,
    tripped_at TEXT NOT NULL,
    resumed_at TEXT
);
CREATE TABLE IF NOT EXISTS prerequisites (
    record_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    incident_id TEXT NOT NULL,
    produced_by TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS correction_authorisations (
    authorisation_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL,
    publication_id TEXT NOT NULL,
    offending_item_id TEXT NOT NULL,
    replacement_text TEXT NOT NULL,
    replacement_sha256 TEXT NOT NULL,
    expected_body_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS corrections (
    correction_id TEXT PRIMARY KEY,
    authorisation_id TEXT NOT NULL UNIQUE,
    publication_id TEXT NOT NULL,
    state TEXT NOT NULL,
    reason TEXT,
    body_version INTEGER,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS publication_bodies (
    publication_id TEXT PRIMARY KEY,
    body_version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS idempotency (
    key TEXT PRIMARY KEY,
    request_sha256 TEXT NOT NULL,
    outcome_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scorecard_rows (
    row_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    task_id TEXT NOT NULL,
    task_class TEXT NOT NULL,
    data_json TEXT NOT NULL,
    at TEXT NOT NULL,
    PRIMARY KEY (row_id, version)
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    event TEXT NOT NULL,
    emitted_at TEXT NOT NULL,
    run_id TEXT,
    authoritative_generation INTEGER,
    report_version INTEGER,
    correction_version INTEGER,
    data_json TEXT NOT NULL
);
"""


def iso(value: datetime) -> str:
    return value.isoformat()


def parse_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class OpsDB:
    """Thread-safe wrapper over one SQLite database with immediate-mode transactions."""

    def __init__(self, path: Path | str = ":memory:") -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(SCHEMA)
        self._add_project_columns()
        self._depth = 0

    # Tables whose rows belong to one project. The project is stored when the row is written and
    # every read or change is checked against it; it is never taken from a request.
    PROJECT_TABLES = ("tasks", "approvals", "scorecard_rows", "correction_authorisations", "corrections")

    def _add_project_columns(self) -> None:
        for table in self.PROJECT_TABLES:
            cols = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if "project_id" not in cols:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN project_id TEXT NOT NULL DEFAULT ''")

    @property
    def lock(self) -> threading.RLock:
        """Held across a publication step so concurrent re-verification is serialised with it."""
        return self._lock

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Open (or join) a transaction. Nested calls join the outer transaction."""
        with self._lock:
            if self._depth:
                self._depth += 1
                try:
                    yield self._conn
                finally:
                    self._depth -= 1
                return
            self._conn.execute("BEGIN IMMEDIATE")
            self._depth = 1
            try:
                yield self._conn
            except BaseException:
                self._depth = 0
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._depth = 0
                self._conn.execute("COMMIT")

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params))

    def one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- event outbox --------------------------------------------------------------------------

    def emit_event(
        self,
        conn: sqlite3.Connection,
        event: str,
        *,
        at: datetime,
        run_id: str | None,
        generation: int | None,
        data: Mapping[str, Any],
        report_version: int | None = None,
        correction_version: int | None = None,
    ) -> str:
        """Append a live event in the caller's transaction. Only names from the shared list."""
        if event not in EVENT_NAMES:
            raise ValueError(f"unknown event name: {event}")
        event_id = new_id("evt")
        conn.execute(
            "INSERT INTO events (event_id, event, emitted_at, run_id, authoritative_generation,"
            " report_version, correction_version, data_json) VALUES (?,?,?,?,?,?,?,?)",
            (event_id, event, iso(at), run_id, generation, report_version, correction_version,
             json.dumps(dict(data), sort_keys=True, default=str)),
        )
        return event_id

    def events(self, name: str | None = None) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT * FROM events" + (" WHERE event = ?" if name else "") + " ORDER BY seq",
            (name,) if name else (),
        )
        return [
            {
                "event": r["event"],
                "event_id": r["event_id"],
                "emitted_at": r["emitted_at"],
                "run_id": r["run_id"],
                "authoritative_generation": r["authoritative_generation"],
                "report_version": r["report_version"],
                "correction_version": r["correction_version"],
                "data": json.loads(r["data_json"]),
            }
            for r in rows
        ]

    # -- idempotency ---------------------------------------------------------------------------

    def idempotent(self, conn: sqlite3.Connection, key: str | None, request: Mapping[str, Any]) -> dict | None:
        """Return the stored outcome for a repeated key, or None if the key is new.

        Reusing a key with a different request is refused.
        """
        if not key:
            return None
        import hashlib

        digest = hashlib.sha256(json.dumps(dict(request), sort_keys=True, default=str).encode()).hexdigest()
        row = conn.execute("SELECT request_sha256, outcome_json FROM idempotency WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != digest:
            raise IdempotencyConflict("Idempotency-Key was reused with a different request")
        return json.loads(row["outcome_json"])

    def remember(self, conn: sqlite3.Connection, key: str | None, request: Mapping[str, Any], outcome: Mapping[str, Any]) -> None:
        if not key:
            return
        import hashlib

        digest = hashlib.sha256(json.dumps(dict(request), sort_keys=True, default=str).encode()).hexdigest()
        conn.execute(
            "INSERT INTO idempotency (key, request_sha256, outcome_json) VALUES (?,?,?)",
            (key, digest, json.dumps(dict(outcome), sort_keys=True, default=str)),
        )


class IdempotencyConflict(ValueError):
    """An Idempotency-Key was reused with a different request body."""
