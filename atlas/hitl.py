"""Human-in-the-loop queue.

Entries are de-duplicated by (trigger, subject id, run id), carry scoped evidence and are always
read through the authorization projection.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from atlas.contracts import Principal

TRIGGERS = frozenset(
    {
        "low_confidence",
        "security_blocked",
        "security_not_run",
        "forecast_slip",
        "requirements_at_risk",
        "verifier_rejections",
        "insufficient_evidence_high_rank",
        "code_change",
        "injection_detected",
        "payload_withheld",
        "unsupported_claim_found",
        "budget_exceeded",
        "nightly_backlog",
        "scorecard_class_negative",
        "support_withdrawn",
        "run_unverified",
        "access_request",
    }
)

CONFIDENCE_THRESHOLD = 0.7
VERIFIER_REJECTION_LIMIT = 2
NIGHTLY_BACKLOG_LIMIT = 10
SLIP_DAYS = 7


class HumanQueue:
    def __init__(self, path: str | Path = ":memory:", clock=lambda: datetime.now(timezone.utc)) -> None:
        self._db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS queue (id TEXT PRIMARY KEY, trig TEXT, subject_id TEXT, run_id TEXT, "
            "reason TEXT, evidence TEXT, required_scopes TEXT, status TEXT, created TEXT, "
            "UNIQUE(trig, subject_id, run_id))"
        )
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(queue)").fetchall()}
        if "project_id" not in cols:
            self._db.execute("ALTER TABLE queue ADD COLUMN project_id TEXT NOT NULL DEFAULT ''")
        self._lock = threading.Lock()
        self.clock = clock

    def add(self, trigger: str, subject_id: str, run_id: str | None, reason: str,
            evidence: Sequence[Mapping[str, Any]] = (), required_scopes: Sequence[str] = ("internal",),
            project_id: str = "") -> tuple[str, bool]:
        """Add an entry; returns (id, created). A repeat of the same trigger, subject and run is ignored."""
        if trigger not in TRIGGERS:
            raise ValueError(f"unknown queue trigger {trigger}")
        with self._lock:
            row = self._db.execute(
                "SELECT id FROM queue WHERE trig = ? AND subject_id = ? AND run_id IS ?", (trigger, subject_id, run_id)
            ).fetchone()
            if row:
                return row[0], False
            entry_id = f"q-{uuid.uuid4().hex[:10]}"
            self._db.execute(
                "INSERT INTO queue (id, trig, subject_id, run_id, reason, evidence, required_scopes, status, created,"
                " project_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (entry_id, trigger, subject_id, run_id, reason, json.dumps(list(evidence), default=str),
                 json.dumps(sorted(set(required_scopes))), "open", self.clock().isoformat(), project_id),
            )
            return entry_id, True

    def entries(self) -> list[dict[str, Any]]:
        rows = self._db.execute("SELECT * FROM queue ORDER BY created").fetchall()
        keys = ("id", "trigger", "subject_id", "run_id", "reason", "evidence", "required_scopes", "status", "created",
                "project_id")
        out = []
        for r in rows:
            d = dict(zip(keys, r))
            d["project_id"] = d.get("project_id") or None
            d["evidence"] = json.loads(d["evidence"])
            d["required_scopes"] = json.loads(d["required_scopes"])
            out.append(d)
        return out

    def view(self, projection: Any, principal: Principal) -> list[dict[str, Any]]:
        return projection.project({"required_scopes": [], "entries": self.entries()}, principal)["entries"]


def triggers_for_run(output: Mapping[str, Any], *, budget_exhausted: str | None = None,
                     payload_withheld: bool = False) -> list[tuple[str, str]]:
    """Box 12 triggers raised by one run's output: (trigger, reason)."""
    found: list[tuple[str, str]] = []
    conf = output.get("confidence")
    if isinstance(conf, (int, float)) and conf < CONFIDENCE_THRESHOLD:
        found.append(("low_confidence", f"confidence {conf} is below {CONFIDENCE_THRESHOLD}"))
    state = (output.get("security_check") or {}).get("state")
    if state == "blocked":
        found.append(("security_blocked", "blocking security finding"))
    elif state == "not_run":
        found.append(("security_not_run", "security check not run"))
    rejected = (output.get("verifier") or {}).get("claims_rejected", 0)
    if rejected > VERIFIER_REJECTION_LIMIT:
        found.append(("verifier_rejections", f"verifier rejected {rejected} claims"))
    if output.get("injection_detected"):
        found.append(("injection_detected", "instruction-like text found in evidence"))
    if output.get("run_status") == "unverified":
        found.append(("run_unverified", "verification did not complete"))
    if budget_exhausted:
        found.append(("budget_exceeded", f"{budget_exhausted} budget exhausted"))
    if payload_withheld:
        found.append(("payload_withheld", "a payload was withheld by the outbound boundary"))
    for entry in output.get("insufficient_evidence", []) or []:
        if entry.get("rank", 99) <= 3:
            found.append(("insufficient_evidence_high_rank", entry.get("text", "insufficient evidence")))
    forecast = output.get("forecast")
    if isinstance(forecast, Mapping):
        if forecast.get("requirements_at_risk"):
            found.append(("requirements_at_risk", "forecast item bound to a next-release requirement"))
        if (forecast.get("slip_days") or 0) > SLIP_DAYS:
            found.append(("forecast_slip", f"forecast slip of {forecast['slip_days']} days"))
    return found
