"""Injection disposition and the feedback store."""

from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from atlas.sqlite_util import connect as safe_connect

TASK_FIELDS = ("task", "task_rationale", "patch", "diff", "code_proposal")


def apply_injection_disposition(artefact: Mapping[str, Any], injection_detected: bool) -> dict[str, Any]:
    """Keep the evidence-based recommendation, but hold a flagged run for a human.

    A flagged run's action becomes REJECT, its safety disposition HOLD_FOR_HUMAN, and no task or
    code proposal survives in any field.
    """
    out = dict(artefact)
    action = (out.get("action") or {}).get("value")
    out["evidence_recommendation"] = action
    out["injection_detected"] = bool(injection_detected)
    if not injection_detected:
        out.setdefault("safety_disposition", "NONE")
        return out
    out["safety_disposition"] = "HOLD_FOR_HUMAN"
    cls = dict(out.get("classification") or {})
    if cls.get("value") is None:
        # Nothing readable supports a classification: say so instead of leaving it empty.
        out["classification"] = {"value": "UNKNOWN", "claim_ids": [],
                                 "required_scopes": list(cls.get("required_scopes", ["public"]))}
    out["action"] = {"value": "REJECT", "claim_ids": [], "required_scopes": list((out.get("action") or {}).get("required_scopes", ["public"]))}
    for name in TASK_FIELDS:
        out.pop(name, None)
    out["options"] = [o for o in out.get("options", []) if "task" not in str(o.get("text", "")).lower() and "patch" not in str(o.get("text", "")).lower()]
    out["claims"] = [c for c in out.get("claims", []) if c.get("kind") != "task_rationale"]
    return out


class FeedbackStore:
    """Append-only override and debt-acceptance records with lineage to the run."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._db = safe_connect(str(path), isolation_level=None, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS feedback (id TEXT PRIMARY KEY, run_id TEXT, kind TEXT, item_id TEXT, "
            "decision TEXT, rationale TEXT, principal TEXT, golden_candidate INTEGER, created TEXT)"
        )
        self._lock = threading.Lock()

    def record_feedback(self, run_id: str, kind: str, item_id: str, decision: str, rationale: str, principal: str) -> dict[str, Any]:
        if kind not in {"override", "debt_acceptance"}:
            raise ValueError("kind must be override or debt_acceptance")
        if not rationale.strip():
            raise ValueError("a rationale is required")
        rec = {
            "id": f"fb-{uuid.uuid4().hex[:10]}",
            "run_id": run_id,
            "kind": kind,
            "item_id": item_id,
            "decision": decision,
            "rationale": rationale,
            "principal": principal,
            "golden_candidate": kind == "override",
            "lineage": [run_id, item_id],
            "created": datetime.now(timezone.utc).isoformat(),
        }
        with self._lock:
            self._db.execute(
                "INSERT INTO feedback VALUES (?,?,?,?,?,?,?,?,?)",
                (rec["id"], run_id, kind, item_id, decision, rationale, principal, int(rec["golden_candidate"]), rec["created"]),
            )
        return rec

    def golden_candidates(self) -> list[dict[str, Any]]:
        rows = self._db.execute("SELECT id, run_id, item_id, decision, rationale FROM feedback WHERE golden_candidate = 1").fetchall()
        return [dict(zip(("id", "run_id", "item_id", "decision", "rationale"), r)) for r in rows]

    def runs_with_decisions(self) -> set[str]:
        """Runs a person has recorded at least one decision for."""
        return {r[0] for r in self._db.execute("SELECT DISTINCT run_id FROM feedback").fetchall()}

    def update(self, *_: Any, **__: Any) -> None:
        raise PermissionError("feedback records are append-only")
