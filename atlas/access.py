"""Access requests: ask to read one item that your role does not normally see.

An item a role cannot read is shown as locked, never hidden, and the person can send a request for
it. Ordinary items are granted at once; sensitive items wait for a decision by the role that owns
them. A grant is always read-only, for exactly one item, for a limited time, and is written to the
audit record. It never allows any change (approve, publish, resume, start a run).

The table of which role reads and owns each kind of item, and which kinds are sensitive, lives
here and nowhere else.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from atlas.sqlite_util import connect as safe_connect

ALL_ROLES = frozenset({"pm", "em", "architect", "security", "oncall", "audit"})
SECURITY_SCOPE = "restricted:security"

DEFAULT_GRANT_HOURS = 24.0
MAX_GRANT_HOURS = 7 * 24.0
REQUEST_LIFETIME = timedelta(hours=72)
EXPIRING_WINDOW = timedelta(hours=1)
MAX_PENDING_PER_USER = 20

PENDING, GRANTED, DENIED, WITHDRAWN, EXPIRED, REVOKED = "pending", "granted", "denied", "withdrawn", "expired", "revoked"


@dataclass(frozen=True)
class KindRule:
    """Who reads a kind of item without asking, who owns it, and whether a person must decide."""

    label: str
    readers: frozenset[str]
    owner: str
    sensitive: bool = False


def _r(*roles: str) -> frozenset[str]:
    return frozenset(roles) | {"audit"}


# The single sensitivity and ownership table. Keys are ``object_type`` or ``object_type:subtype``.
KINDS: dict[str, KindRule] = {
    "run": KindRule("Run", ALL_ROLES, "em"),
    "report:forecast": KindRule("Forecast report", _r("pm"), "pm"),
    "report:allocation": KindRule("Allocation report", _r("em"), "em"),
    "report:architecture": KindRule("Architecture report", _r("architect"), "architect"),
    "report:investigation": KindRule("Investigation report", _r("oncall", "em", "security"), "em"),
    "report:question": KindRule("Answer", ALL_ROLES, "pm"),
    "report:scorecard": KindRule("Scorecard", _r("em"), "em"),
    "report:security_gate": KindRule("Security report", _r("security"), "security", sensitive=True),
    "task": KindRule("Task", _r("em"), "em"),
    "approval": KindRule("Approval", _r("em"), "em"),
    "scorecard_row": KindRule("Scorecard row", _r("em"), "em"),
    "graph": KindRule("Knowledge graph", _r("architect"), "architect"),
    "evidence": KindRule("Evidence record", ALL_ROLES, "em"),
    "finding": KindRule("Security finding", _r("security"), "security", sensitive=True),
    "incident": KindRule("Incident", _r("security"), "audit", sensitive=True),
    "kill_switch": KindRule("Kill switch", _r("security"), "audit", sensitive=True),
    "dead_letter": KindRule("Failed deliveries", _r(), "audit", sensitive=True),
}


def kind_key(object_type: str, subtype: str | None = None) -> str:
    return f"{object_type}:{subtype}" if subtype else object_type


@dataclass(frozen=True)
class Policy:
    """The rule that applies to one concrete item, after looking at its scopes."""

    label: str
    readers: frozenset[str]
    owner: str
    sensitive: bool
    grantable: bool


def policy_for(object_type: str, subtype: str | None, scopes: Sequence[str] | None) -> Policy | None:
    """The policy for an item, or None for a kind that is not in the table.

    Content with unknown scopes can never be granted. Anything that itself carries the restricted
    security scope is sensitive and owned by the security role, whatever its kind.
    """
    rule = KINDS.get(kind_key(object_type, subtype)) or KINDS.get(object_type)
    if rule is None:
        return None
    if scopes is None:
        return Policy(rule.label, frozenset(), rule.owner, True, False)
    if SECURITY_SCOPE in set(scopes):
        return Policy(rule.label, rule.readers & frozenset({"security", "audit"}), "security", True, True)
    return Policy(rule.label, rule.readers, rule.owner, rule.sensitive, True)


class AccessError(Exception):
    def __init__(self, status: int, error: str, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.error = error
        self.reason = reason


def requester_key(user: str | None, role: str) -> str:
    """Who a request belongs to: the signed-in person, or the demo role when there is no sign-in."""
    return f"user:{user}" if user else f"role:{role}"


SCHEMA = """
CREATE TABLE IF NOT EXISTS access_requests (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, requester TEXT NOT NULL, requester_user TEXT,
  requester_role TEXT NOT NULL, object_type TEXT NOT NULL, object_id TEXT NOT NULL, label TEXT NOT NULL,
  owner_role TEXT NOT NULL, sensitive INTEGER NOT NULL, reason TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS access_decisions (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL, at TEXT NOT NULL, action TEXT NOT NULL,
  by_user TEXT, by_role TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS access_grants (
  id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, granted_by_user TEXT, granted_by_role TEXT NOT NULL,
  automatic INTEGER NOT NULL, read_only INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL, expiring_notified INTEGER NOT NULL DEFAULT 0);
CREATE TRIGGER IF NOT EXISTS access_decisions_no_update BEFORE UPDATE ON access_decisions
  BEGIN SELECT RAISE(ABORT, 'access decisions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS access_decisions_no_delete BEFORE DELETE ON access_decisions
  BEGIN SELECT RAISE(ABORT, 'access decisions are append-only'); END;
"""

_REQ_COLS = ("id", "project_id", "requester", "requester_user", "requester_role", "object_type", "object_id",
             "label", "owner_role", "sensitive", "reason", "created_at")
_GRANT_COLS = ("id", "request_id", "granted_by_user", "granted_by_role", "automatic", "read_only", "created_at",
               "expires_at", "expiring_notified")


class AccessRequestStore:
    """Requests, their decisions (append-only) and grants, kept apart per project."""

    def __init__(self, path: str | Path = ":memory:",
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self._db = safe_connect(str(path), isolation_level=None, check_same_thread=False)
        self._db.executescript(SCHEMA)
        self._lock = threading.RLock()
        self.clock = clock

    # ------------------------------------------------------------------ reading

    def _decisions(self, request_id: str) -> list[dict[str, Any]]:
        rows = self._db.execute("SELECT at, action, by_user, by_role, detail FROM access_decisions "
                                "WHERE request_id = ? ORDER BY seq", (request_id,)).fetchall()
        return [{"at": r[0], "action": r[1], "by_user": r[2], "by_role": r[3], "detail": json.loads(r[4] or "{}")}
                for r in rows]

    def _grant_row(self, request_id: str) -> dict[str, Any] | None:
        row = self._db.execute(f"SELECT {', '.join(_GRANT_COLS)} FROM access_grants WHERE request_id = ?",
                               (request_id,)).fetchone()
        return dict(zip(_GRANT_COLS, row)) if row else None

    def _view(self, row: Sequence[Any]) -> dict[str, Any]:
        req = dict(zip(_REQ_COLS, row))
        req["sensitive"] = bool(req["sensitive"])
        decisions = self._decisions(req["id"])
        last = decisions[-1]["action"] if decisions else None
        now = self.clock()
        grant = self._grant_row(req["id"])
        status = PENDING
        if last in (DENIED, WITHDRAWN, REVOKED):
            status = last
        elif grant is not None and last in ("granted", "granted_automatically"):
            status = EXPIRED if datetime.fromisoformat(grant["expires_at"]) <= now else GRANTED
        elif datetime.fromisoformat(req["created_at"]) + REQUEST_LIFETIME <= now:
            status = EXPIRED
        req["request_id"] = req.pop("id")
        req["status"] = status
        req["decisions"] = decisions
        req["grant"] = None
        if grant is not None:
            req["grant"] = {"grant_id": grant["id"], "request_id": grant["request_id"],
                            "granted_by": {"user": grant["granted_by_user"], "role": grant["granted_by_role"]},
                            "automatic": bool(grant["automatic"]), "read_only": True,
                            "created_at": grant["created_at"], "expires_at": grant["expires_at"],
                            "revoked_at": next((d["at"] for d in decisions if d["action"] == REVOKED), None),
                            "active": status == GRANTED}
        return req

    def get(self, request_id: str) -> dict[str, Any] | None:
        row = self._db.execute(f"SELECT {', '.join(_REQ_COLS)} FROM access_requests WHERE id = ?",
                               (request_id,)).fetchone()
        return self._view(row) if row else None

    def by_grant(self, grant_id: str) -> dict[str, Any] | None:
        row = self._db.execute("SELECT request_id FROM access_grants WHERE id = ?", (grant_id,)).fetchone()
        return self.get(row[0]) if row else None

    def _all(self, where: str, args: Sequence[Any]) -> list[dict[str, Any]]:
        rows = self._db.execute(f"SELECT {', '.join(_REQ_COLS)} FROM access_requests WHERE {where} "
                                "ORDER BY created_at", tuple(args)).fetchall()
        return [self._view(r) for r in rows]

    def mine(self, requester: str, project_id: str) -> list[dict[str, Any]]:
        return self._all("requester = ? AND project_id = ?", (requester, project_id))

    def to_decide(self, owner_role: str, project_id: str) -> list[dict[str, Any]]:
        """Requests and grants the owning role looks after: pending ones first, then live grants."""
        items = self._all("owner_role = ? AND project_id = ?", (owner_role, project_id))
        return [i for i in items if i["status"] in (PENDING, GRANTED)]

    def latest_for(self, requester: str, project_id: str, object_type: str, object_id: str) -> dict[str, Any] | None:
        items = self._all("requester = ? AND project_id = ? AND object_type = ? AND object_id = ?",
                          (requester, project_id, object_type, object_id))
        return items[-1] if items else None

    def active_grant(self, requester: str, project_id: str, object_type: str, object_id: str) -> dict[str, Any] | None:
        """The live read-only grant for exactly this person, project and item, if there is one."""
        latest = self.latest_for(requester, project_id, object_type, object_id)
        if latest is None or latest["status"] != GRANTED:
            return None
        return latest["grant"]

    # ------------------------------------------------------------------ writing

    def _decision(self, request_id: str, action: str, by_user: str | None, by_role: str | None,
                  detail: Mapping[str, Any] | None = None) -> None:
        self._db.execute("INSERT INTO access_decisions (request_id, at, action, by_user, by_role, detail) "
                         "VALUES (?,?,?,?,?,?)", (request_id, self.clock().isoformat(), action, by_user, by_role,
                                                  json.dumps(dict(detail or {}), sort_keys=True)))

    def create(self, *, project_id: str, user: str | None, role: str, object_type: str, object_id: str,
               label: str, owner_role: str, sensitive: bool, reason: str | None) -> tuple[dict[str, Any], bool]:
        """Record a request. A repeat while one is pending or granted returns that one."""
        who = requester_key(user, role)
        with self._lock:
            latest = self.latest_for(who, project_id, object_type, object_id)
            if latest is not None and latest["status"] in (PENDING, GRANTED):
                return latest, False
            pending = [r for r in self._all("requester = ?", (who,)) if r["status"] == PENDING]
            if len(pending) >= MAX_PENDING_PER_USER:
                raise AccessError(429, "too_many_requests",
                                  f"you already have {MAX_PENDING_PER_USER} requests waiting for a decision")
            rid = f"req-{uuid.uuid4().hex[:10]}"
            self._db.execute(
                "INSERT INTO access_requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, project_id, who, user, role, object_type, object_id, label, owner_role, int(sensitive),
                 (reason or "")[:500] or None, self.clock().isoformat()))
            self._decision(rid, "requested", user, role)
            return self.get(rid), True  # type: ignore[return-value]

    def grant(self, request_id: str, *, by_user: str | None, by_role: str, hours: float | None = None,
              automatic: bool = False) -> dict[str, Any]:
        hours = DEFAULT_GRANT_HOURS if hours is None else float(hours)
        if not 0 < hours <= MAX_GRANT_HOURS:
            raise AccessError(400, "invalid_hours", f"hours must be more than 0 and at most {int(MAX_GRANT_HOURS)}")
        with self._lock:
            req = self.get(request_id)
            if req is None:
                raise AccessError(404, "not_found", "request not found")
            if req["status"] != PENDING:
                raise AccessError(409, "not_pending", f"this request is {req['status']}")
            now = self.clock()
            self._db.execute(
                "INSERT INTO access_grants (id, request_id, granted_by_user, granted_by_role, automatic, read_only, "
                "created_at, expires_at) VALUES (?,?,?,?,?,1,?,?)",
                (f"grant-{uuid.uuid4().hex[:10]}", request_id, by_user, by_role, int(automatic), now.isoformat(),
                 (now + timedelta(hours=hours)).isoformat()))
            self._decision(request_id, "granted_automatically" if automatic else "granted", by_user, by_role,
                           {"hours": hours})
            return self.get(request_id)  # type: ignore[return-value]

    def _close(self, request_id: str, action: str, allowed: tuple[str, ...], by_user: str | None,
               by_role: str | None) -> dict[str, Any]:
        with self._lock:
            req = self.get(request_id)
            if req is None:
                raise AccessError(404, "not_found", "request not found")
            if req["status"] not in allowed:
                raise AccessError(409, "not_" + allowed[0], f"this request is {req['status']}")
            self._decision(request_id, action, by_user, by_role)
            return self.get(request_id)  # type: ignore[return-value]

    def deny(self, request_id: str, *, by_user: str | None, by_role: str) -> dict[str, Any]:
        return self._close(request_id, DENIED, (PENDING,), by_user, by_role)

    def withdraw(self, request_id: str, *, by_user: str | None, by_role: str) -> dict[str, Any]:
        return self._close(request_id, WITHDRAWN, (PENDING,), by_user, by_role)

    def revoke(self, request_id: str, *, by_user: str | None, by_role: str) -> dict[str, Any]:
        return self._close(request_id, REVOKED, (GRANTED,), by_user, by_role)

    def expiring(self) -> list[dict[str, Any]]:
        """Live grants that end within the next hour and have not had a reminder yet (marks them)."""
        now = self.clock()
        out = []
        with self._lock:
            rows = self._db.execute("SELECT request_id FROM access_grants WHERE expiring_notified = 0").fetchall()
            for (rid,) in rows:
                req = self.get(rid)
                if req and req["status"] == GRANTED and \
                        datetime.fromisoformat(req["grant"]["expires_at"]) - now <= EXPIRING_WINDOW:
                    self._db.execute("UPDATE access_grants SET expiring_notified = 1 WHERE request_id = ?", (rid,))
                    out.append(req)
        return out
