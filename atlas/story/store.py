"""Durable records for hand-offs, product requests, work splits and coding-agent timelines.

Every record carries the project it belongs to. Reads filter by project; the API answers a record
of another project exactly like a missing one.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from atlas.sqlite_util import connect as safe_connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS handoffs (
  handoff_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, subject_type TEXT, subject_id TEXT, subject_title TEXT,
  run_id TEXT, report_id TEXT, from_role TEXT, from_user TEXT, to_role TEXT, note TEXT, status TEXT,
  reply TEXT, task_id TEXT, created TEXT, updated TEXT, decision TEXT);
CREATE TABLE IF NOT EXISTS requests (
  request_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, kind TEXT, title TEXT, description TEXT,
  link_type TEXT, link_id TEXT, link_label TEXT, owner_role TEXT, info_role TEXT, from_role TEXT,
  from_user TEXT, status TEXT, reply TEXT, answered_by TEXT, created TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS splits (
  split_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, run_id TEXT, handoff_id TEXT, parts TEXT,
  recommendation TEXT, override TEXT, task_id TEXT, confirmed_by TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS stories (
  task_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, run_id TEXT, handoff_id TEXT, split_id TEXT,
  human_hours_low REAL, human_hours_high REAL, status TEXT, error TEXT, spend_before REAL,
  cost_usd REAL, pr_number INTEGER, pr_url TEXT, started TEXT, finished TEXT, signed_off_by TEXT,
  signed_off_at TEXT, created TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS steps (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, step TEXT, label TEXT, status TEXT,
  started TEXT, ended TEXT, detail TEXT);
"""

HANDOFF_STATUSES = ("waiting", "accepted", "done", "declined")
HANDOFF_DECISIONS = ("coding_agent", "oncall")
"""How the engineering manager delegates a hand-off: to the coding agent or to the on-call engineer."""
REQUEST_STATUSES = ("new", "accepted", "needs_more_info", "declined")
REQUEST_KINDS = {"technical_dependency": ("architect", "em"), "new_feature": ("em", "architect")}
"""Request kind -> (owner role, role that gets it for information)."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class StoryStore:
    def __init__(self, path: str | Path = ":memory:", clock: Callable[[], datetime] = _now) -> None:
        self._db = safe_connect(str(path), isolation_level=None, check_same_thread=False)
        self._db.executescript(SCHEMA)
        self._lock = threading.RLock()
        self.clock = clock

    # ------------------------------------------------------------------ helpers

    def _rows(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._db.execute(sql, tuple(params))
            names = [d[0] for d in cur.description]
            return [dict(zip(names, r)) for r in cur.fetchall()]

    def _one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        rows = self._rows(sql, params)
        return rows[0] if rows else None

    def _exec(self, sql: str, params: Sequence[Any] = ()) -> None:
        with self._lock:
            self._db.execute(sql, tuple(params))

    @staticmethod
    def _in_project(sql: str, project_id: str | None) -> tuple[str, list[Any]]:
        if project_id is None:
            return sql, []
        return sql + (" AND" if " WHERE " in sql else " WHERE") + " project_id = ?", [project_id]

    # ------------------------------------------------------------------ hand-offs

    def add_handoff(self, *, project_id: str, subject_type: str, subject_id: str, subject_title: str,
                    run_id: str | None, report_id: str | None, from_role: str, from_user: str | None,
                    to_role: str, note: str) -> dict[str, Any]:
        hid = _id("hand")
        now = self.clock().isoformat()
        self._exec("INSERT INTO handoffs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (hid, project_id, subject_type, subject_id, subject_title, run_id, report_id, from_role,
                    from_user, to_role, note, "waiting", None, None, now, now, None))
        return self.handoff(hid)  # type: ignore[return-value]

    def handoff(self, handoff_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM handoffs WHERE handoff_id = ?", (handoff_id,))

    def handoffs(self, project_id: str | None = None) -> list[dict[str, Any]]:
        sql, params = self._in_project("SELECT * FROM handoffs", project_id)
        return self._rows(sql + " ORDER BY created DESC", params)

    def set_handoff(self, handoff_id: str, **fields: Any) -> dict[str, Any]:
        allowed = {"status", "reply", "task_id", "decision"}
        if set(fields) - allowed:
            raise ValueError(f"fields not updatable: {sorted(set(fields) - allowed)}")
        sets = ", ".join(f"{k} = ?" for k in fields) + ", updated = ?"
        self._exec(f"UPDATE handoffs SET {sets} WHERE handoff_id = ?",
                   (*fields.values(), self.clock().isoformat(), handoff_id))
        return self.handoff(handoff_id)  # type: ignore[return-value]

    # ------------------------------------------------------------------ product requests

    def add_request(self, *, project_id: str, kind: str, title: str, description: str, link_type: str | None,
                    link_id: str | None, link_label: str | None, from_role: str,
                    from_user: str | None) -> dict[str, Any]:
        owner, info = REQUEST_KINDS[kind]
        rid = _id("req")
        now = self.clock().isoformat()
        self._exec("INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (rid, project_id, kind, title, description, link_type, link_id, link_label, owner, info,
                    from_role, from_user, "new", None, None, now, now))
        return self.request(rid)  # type: ignore[return-value]

    def request(self, request_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM requests WHERE request_id = ?", (request_id,))

    def requests(self, project_id: str | None = None) -> list[dict[str, Any]]:
        sql, params = self._in_project("SELECT * FROM requests", project_id)
        return self._rows(sql + " ORDER BY created DESC", params)

    def answer_request(self, request_id: str, status: str, reply: str, by: str) -> dict[str, Any]:
        self._exec("UPDATE requests SET status = ?, reply = ?, answered_by = ?, updated = ? WHERE request_id = ?",
                   (status, reply, by, self.clock().isoformat(), request_id))
        return self.request(request_id)  # type: ignore[return-value]

    # ------------------------------------------------------------------ splits

    def add_split(self, *, project_id: str, run_id: str, handoff_id: str | None, parts: Sequence[Mapping[str, Any]],
                  recommendation: Mapping[str, Any], override: Mapping[str, Any] | None, task_id: str | None,
                  confirmed_by: str) -> dict[str, Any]:
        sid = _id("split")
        self._exec("INSERT INTO splits VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (sid, project_id, run_id, handoff_id, json.dumps(list(parts)), json.dumps(dict(recommendation)),
                    json.dumps(dict(override)) if override else None, task_id, confirmed_by,
                    self.clock().isoformat()))
        return self.split(sid)  # type: ignore[return-value]

    def split(self, split_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM splits WHERE split_id = ?", (split_id,))
        if row:
            row["parts"] = json.loads(row["parts"] or "[]")
            row["recommendation"] = json.loads(row["recommendation"] or "{}")
            row["override"] = json.loads(row["override"]) if row["override"] else None
        return row

    def split_for_task(self, task_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT split_id FROM splits WHERE task_id = ?", (task_id,))
        return self.split(row["split_id"]) if row else None

    def splits_for(self, *, run_id: str | None = None, handoff_id: str | None = None) -> list[dict[str, Any]]:
        if handoff_id:
            ids = self._rows("SELECT split_id FROM splits WHERE handoff_id = ? ORDER BY created DESC", (handoff_id,))
        else:
            ids = self._rows("SELECT split_id FROM splits WHERE run_id = ? ORDER BY created DESC", (run_id,))
        return [s for s in (self.split(r["split_id"]) for r in ids) if s]

    # ------------------------------------------------------------------ stories and steps

    def add_story(self, *, task_id: str, project_id: str, run_id: str, handoff_id: str | None, split_id: str | None,
                  hours: tuple[float, float] | None) -> dict[str, Any]:
        now = self.clock().isoformat()
        self._exec("INSERT OR IGNORE INTO stories (task_id, project_id, run_id, handoff_id, split_id, human_hours_low,"
                   " human_hours_high, status, created, updated) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (task_id, project_id, run_id, handoff_id, split_id, hours[0] if hours else None,
                    hours[1] if hours else None, "ready", now, now))
        return self.story(task_id)  # type: ignore[return-value]

    def story(self, task_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM stories WHERE task_id = ?", (task_id,))

    def stories(self, project_id: str | None = None) -> list[dict[str, Any]]:
        sql, params = self._in_project("SELECT * FROM stories", project_id)
        return self._rows(sql + " ORDER BY created DESC", params)

    def set_story(self, task_id: str, **fields: Any) -> dict[str, Any]:
        allowed = {"status", "error", "spend_before", "cost_usd", "pr_number", "pr_url", "started", "finished",
                   "signed_off_by", "signed_off_at"}
        if set(fields) - allowed:
            raise ValueError(f"fields not updatable: {sorted(set(fields) - allowed)}")
        sets = ", ".join(f"{k} = ?" for k in fields) + ", updated = ?"
        self._exec(f"UPDATE stories SET {sets} WHERE task_id = ?", (*fields.values(), self.clock().isoformat(), task_id))
        return self.story(task_id)  # type: ignore[return-value]

    def claim_status(self, task_id: str, expect: Sequence[str], to: str) -> bool:
        """Move a story from one of ``expect`` to ``to`` in one step; False if another caller got there first."""
        with self._lock:
            marks = ",".join("?" for _ in expect)
            cur = self._db.execute(f"UPDATE stories SET status = ?, updated = ? WHERE task_id = ? AND status IN ({marks})",
                                   (to, self.clock().isoformat(), task_id, *expect))
            return cur.rowcount == 1

    def start_step(self, task_id: str, step: str, label: str, detail: Mapping[str, Any] | None = None,
                   status: str = "running") -> int:
        with self._lock:
            cur = self._db.execute("INSERT INTO steps (task_id, step, label, status, started, ended, detail)"
                                   " VALUES (?,?,?,?,?,?,?)",
                                   (task_id, step, label, status, self.clock().isoformat(), None,
                                    json.dumps(dict(detail or {}), default=str)))
            return int(cur.lastrowid)

    def end_step(self, seq: int, status: str, detail: Mapping[str, Any] | None = None) -> None:
        with self._lock:
            row = self._db.execute("SELECT detail FROM steps WHERE seq = ?", (seq,)).fetchone()
            merged = {**json.loads(row[0] or "{}"), **dict(detail or {})} if row else dict(detail or {})
            self._db.execute("UPDATE steps SET status = ?, ended = ?, detail = ? WHERE seq = ?",
                             (status, self.clock().isoformat(), json.dumps(merged, default=str), seq))

    def steps(self, task_id: str) -> list[dict[str, Any]]:
        rows = self._rows("SELECT * FROM steps WHERE task_id = ? ORDER BY seq", (task_id,))
        for r in rows:
            r["detail"] = json.loads(r["detail"] or "{}")
        return rows

    def finished_steps(self) -> list[dict[str, Any]]:
        """Steps that ended well, from every task: the history the time estimate learns from."""
        return self._rows("SELECT step, started, ended FROM steps WHERE status = 'passed' AND ended IS NOT NULL")
