"""Approvals: append-only, bound, expiring authorisations for a coding-agent task.

Approval A authorises preparing a task; Approval B authorises publishing one exact artefact. Each
binds the task, destination, refs, verification generation and security decision (B also binds
the frozen artefact hashes). Approvals are never rewritten: a changed binding can only be met by a
new approval. Any mismatch, expiry or supersession leaves the approval unusable.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Mapping, TYPE_CHECKING

from atlas.contracts import ApprovalKind, Role, TaskState
from atlas.execution.db import OpsDB, iso, new_id, parse_iso
from atlas.tasks import Task, TaskStore

if TYPE_CHECKING:  # pragma: no cover
    from atlas.containment import ContainmentStore
    from atlas.eligibility import Eligibility

APPROVAL_LIFETIME = timedelta(hours=48)
SLA = timedelta(hours=4)
NEARING_EXPIRY = timedelta(hours=1)



# The main roles may each give an approval; the record keeps who gave it.
APPROVER_ROLES = frozenset({"pm", "em", "architect"})

class ApprovalError(ValueError):
    pass


def bindings_for(task: Task, kind: ApprovalKind, approval_a_id: str | None = None) -> dict[str, Any]:
    base = {
        "task_id": task.task_id,
        "task_text": task.task_text,
        "permitted_scope": list(task.scope),
        "destination_repo": task.destination_repo,
        "destination_node_id": task.destination_node_id,
        "base_ref": task.base_ref,
        "base_commit": task.base_commit,
        "publication_ref": task.publication_ref,
        "action_id": task.action_id,
        "snapshot_id": task.snapshot_id,
        "policy_version": task.policy_version,
        "generation": task.generation,
        "security_decision_id": task.security_decision_id,
        "executable_action": task.executable_action,
        "override": dict(task.override) if task.override else None,
    }
    if kind == ApprovalKind.B:
        artefact = task.artefact or {}
        base["artefact_diff_sha256"] = artefact.get("diff_sha256")
        base["artefact_tree_sha256"] = artefact.get("tree_sha256")
        base["approval_a_id"] = approval_a_id
    return base


class ApprovalService:
    def __init__(self, db: OpsDB, tasks: TaskStore, notifier: Any = None,
                 containment: "ContainmentStore | None" = None) -> None:
        self.db = db
        self.tasks = tasks
        self.notifier = notifier
        self.containment = containment

    # -- reads ---------------------------------------------------------------------------------

    def rows(self, task_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.query("SELECT * FROM approvals WHERE task_id = ? ORDER BY created_at", (task_id,))]

    def current(self, task_id: str, kind: ApprovalKind) -> dict[str, Any] | None:
        row = self.db.one(
            "SELECT * FROM approvals WHERE task_id = ? AND kind = ? AND status = 'granted' ORDER BY created_at DESC LIMIT 1",
            (task_id, kind.value),
        )
        return dict(row) if row else None

    def problems(self, task: Task, kind: ApprovalKind, now: datetime) -> list[str]:
        """Why the task's current approval of this kind cannot be used now (empty list = usable)."""
        row = self.current(task.task_id, kind)
        if row is None:
            return [f"no valid Approval {kind.value}"]
        out: list[str] = []
        if parse_iso(row["expires_at"]) <= now:  # type: ignore[operator]
            out.append(f"Approval {kind.value} expired")
        a_id = None
        if kind == ApprovalKind.B:
            a = self.current(task.task_id, ApprovalKind.A)
            a_id = a["approval_id"] if a else None
        expected = bindings_for(task, kind, a_id)
        bound = json.loads(row["bindings_json"])
        changed = sorted(k for k in expected if bound.get(k) != expected[k])
        if changed:
            out.append(f"Approval {kind.value} bindings no longer match: {', '.join(changed)}")
        return out

    # -- grants --------------------------------------------------------------------------------

    def _insert(self, conn: sqlite3.Connection, task: Task, kind: ApprovalKind, role: str, now: datetime,
                approval_a_id: str | None = None) -> str:
        approval_id = new_id(f"apr{kind.value.lower()}")
        conn.execute(
            "INSERT INTO approvals (approval_id, task_id, run_id, kind, generation, bindings_json, approver_role,"
            " status, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (approval_id, task.task_id, task.run_id, kind.value, task.generation,
             json.dumps(bindings_for(task, kind, approval_a_id), sort_keys=True, default=str), role, "granted",
             iso(now), iso(now + APPROVAL_LIFETIME)),
        )
        return approval_id

    def grant_a(self, task_id: str, *, role: str, eligibility: "Eligibility", now: datetime,
                idempotency_key: str | None = None) -> str:
        if role not in APPROVER_ROLES:
            raise ApprovalError("Approval A is given by the product manager, engineering manager or architect role")
        request = {"op": "approval_a", "task_id": task_id}
        with self.db.transaction() as conn:
            stored = self.db.idempotent(conn, idempotency_key, request)
            if stored is not None:
                return stored["approval_id"]
            task = self.tasks.get(task_id)
            if task.state not in (TaskState.PROPOSED, TaskState.AWAITING_REAPPROVAL):
                raise ApprovalError(f"Approval A cannot be given in state {task.state.value}")
            verdict = eligibility.evaluate(task_id, "approval_a", now)
            if not verdict.allowed:
                raise ApprovalError("; ".join(verdict.reasons))
            approval_id = self._insert(conn, task, ApprovalKind.A, role, now)
            self.tasks.transition(task_id, TaskState.APPROVED_A, f"Approval A {approval_id}", now, conn=conn)
            self.db.remember(conn, idempotency_key, request, {"approval_id": approval_id})
        return approval_id

    def grant_b(self, task_id: str, *, role: str, eligibility: "Eligibility", now: datetime,
                idempotency_key: str | None = None) -> str:
        if role not in APPROVER_ROLES:
            raise ApprovalError("Approval B is given by the product manager, engineering manager or architect role")
        request = {"op": "approval_b", "task_id": task_id}
        with self.db.transaction() as conn:
            stored = self.db.idempotent(conn, idempotency_key, request)
            if stored is not None:
                return stored["approval_id"]
            task = self.tasks.get(task_id)
            if task.state != TaskState.PREPARED:
                raise ApprovalError(f"Approval B cannot be given in state {task.state.value}")
            verdict = eligibility.evaluate(task_id, "approval_b", now)
            if not verdict.allowed:
                raise ApprovalError("; ".join(verdict.reasons))
            a = self.current(task_id, ApprovalKind.A)
            approval_id = self._insert(conn, task, ApprovalKind.B, role, now, a["approval_id"] if a else None)
            self.tasks.transition(task_id, TaskState.APPROVED_B, f"Approval B {approval_id}", now, conn=conn)
            self.db.remember(conn, idempotency_key, request, {"approval_id": approval_id})
        return approval_id

    # -- invalidation --------------------------------------------------------------------------

    def _void_rows(self, conn: sqlite3.Connection, rows: list[sqlite3.Row], reason: str, now: datetime) -> list[str]:
        voided = []
        for r in rows:
            conn.execute("UPDATE approvals SET status = 'voided', voided_at = ?, void_reason = ? WHERE approval_id = ?",
                         (iso(now), reason, r["approval_id"]))
            self.db.emit_event(conn, "approval.voided", at=now, run_id=r["run_id"], generation=r["generation"],
                               data={"approval_id": r["approval_id"], "task_id": r["task_id"], "kind": r["kind"],
                                     "reason": reason})
            voided.append(r["approval_id"])
        return voided

    def void_superseded(self, run_id: str, new_generation: int, *, conn: sqlite3.Connection | None = None,
                        now: datetime | None = None) -> list[str]:
        """Void every granted approval for ``run_id`` bound to a generation lower than
        ``new_generation``. Runs in the caller's transaction; does not change task state.
        Idempotent: a repeat finds nothing left to void and emits nothing."""
        when = now or datetime.now().astimezone()
        with self.db.transaction() as c:
            c = conn or c
            rows = c.execute(
                "SELECT * FROM approvals WHERE run_id = ? AND generation < ? AND status = 'granted'",
                (run_id, new_generation),
            ).fetchall()
            return self._void_rows(c, rows, f"superseded by generation {new_generation}", when)

    def void_task(self, task_id: str, reason: str, now: datetime, *, conn: sqlite3.Connection | None = None,
                  kinds: tuple[str, ...] = ("A", "B")) -> list[str]:
        with self.db.transaction() as c:
            c = conn or c
            marks = ",".join("?" for _ in kinds)
            rows = c.execute(
                f"SELECT * FROM approvals WHERE task_id = ? AND status = 'granted' AND kind IN ({marks})",
                (task_id, *kinds),
            ).fetchall()
            return self._void_rows(c, rows, reason, now)

    def verify_destination(self, task: Task, actual_node_id: str, actual_full_name: str, actual_ref: str,
                           now: datetime) -> list[str]:
        """Compare the gateway's actual destination with the bound one; a mismatch voids both
        approvals and trips containment row 1."""
        problems = []
        if actual_node_id != task.destination_node_id or actual_full_name.lower() != task.destination_repo.lower():
            problems.append("destination repository differs from the approved one")
        if actual_ref != task.publication_ref:
            problems.append("publication ref differs from the approved one")
        if problems:
            self.void_task(task.task_id, "destination mismatch", now)
            if self.containment is not None:
                self.containment.trip(1, producer="gateway", now=now,
                                      detail={"task_id": task.task_id, "run_id": task.run_id, "problems": problems})
        return problems

    # -- reminders and expiry -------------------------------------------------------------------

    def sweep(self, now: datetime) -> dict[str, list[str]]:
        """Expire approvals, send nearing-expiry reminders, and flag waiting tasks past the SLA.
        ATLAS takes no action on a missed SLA; it only reminds."""
        result: dict[str, list[str]] = {"expired": [], "nearing_expiry": [], "sla_missed": []}
        with self.db.transaction() as conn:
            for r in conn.execute("SELECT * FROM approvals WHERE status = 'granted'").fetchall():
                expires = parse_iso(r["expires_at"])
                if expires <= now:  # type: ignore[operator]
                    conn.execute("UPDATE approvals SET status = 'expired' WHERE approval_id = ?", (r["approval_id"],))
                    result["expired"].append(r["approval_id"])
                    self._notify("approval.expired", r["task_id"])
                elif expires - now <= NEARING_EXPIRY:  # type: ignore[operator]
                    result["nearing_expiry"].append(r["approval_id"])
                    self._notify("approval.nearing_expiry", r["task_id"])
            waiting = (TaskState.PROPOSED.value, TaskState.PREPARED.value, TaskState.AWAITING_REAPPROVAL.value)
            for t in conn.execute(
                f"SELECT task_id, updated_at FROM tasks WHERE state IN ({','.join('?' for _ in waiting)})", waiting
            ).fetchall():
                if now - parse_iso(t["updated_at"]) >= SLA:  # type: ignore[operator]
                    result["sla_missed"].append(t["task_id"])
                    self._notify("approval.sla_missed", t["task_id"])
        return result

    def _notify(self, event_type: str, subject: str) -> None:
        if self.notifier is not None:
            self.notifier.emit(event_type, subject, [subject], frozenset({"public"}))
