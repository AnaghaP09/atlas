"""Operational task store: the single authoritative source of task lifecycle state.

A task is created in ``proposed`` with a stable action id and its first security decision. Its
state, support status, bound verification generation, approvals, preparation checks, frozen
artefact hashes and publication journal all live here, before and after publication. Raw security
evidence and post-publication CI observations live elsewhere and are never task state.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

from atlas.contracts import (
    PUBLISHED_STATES,
    UNPUBLISHED_STATES,
    Action,
    SecurityContext,
    SecurityState,
    SupportStatus,
    TaskState,
)
from atlas.execution.db import OpsDB, iso, new_id, parse_iso
from atlas.execution.ports import SecurityGate, SecurityScanner

DEFECT_WINDOW = timedelta(days=30)

ALLOWED_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.PROPOSED: frozenset({TaskState.APPROVED_A, TaskState.VOIDED}),
    TaskState.APPROVED_A: frozenset({TaskState.PREPARED, TaskState.AWAITING_REAPPROVAL, TaskState.VOIDED}),
    TaskState.PREPARED: frozenset(
        {TaskState.APPROVED_B, TaskState.APPROVED_A, TaskState.AWAITING_REAPPROVAL, TaskState.VOIDED}
    ),
    TaskState.APPROVED_B: frozenset(
        {TaskState.PUBLISHED, TaskState.APPROVED_A, TaskState.AWAITING_REAPPROVAL, TaskState.VOIDED}
    ),
    TaskState.PUBLISHED: frozenset({TaskState.AWAITING_VALIDATION, TaskState.FAILED}),
    TaskState.AWAITING_VALIDATION: frozenset({TaskState.ACCEPTED, TaskState.FAILED}),
    TaskState.AWAITING_REAPPROVAL: frozenset({TaskState.APPROVED_A, TaskState.VOIDED}),
    TaskState.ACCEPTED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.VOIDED: frozenset(),
}


class TaskError(ValueError):
    """A task operation is not allowed in the task's current state."""


@dataclass(frozen=True)
class Task:
    task_id: str
    action_id: str
    run_id: str
    generation: int
    action: str
    task_class: str
    task_text: str
    scope: tuple[str, ...]
    destination_repo: str
    destination_node_id: str
    base_ref: str
    base_commit: str
    publication_ref: str
    snapshot_id: str
    policy_version: str
    state: TaskState
    support_status: str | None
    security_decision_id: str | None
    override: Mapping[str, Any] | None
    artefact: Mapping[str, Any] | None
    checks: Mapping[str, Any]
    published_commit: str | None
    pr_number: int | None
    published_at: datetime | None
    defect_window_ends: datetime | None
    created_at: datetime
    updated_at: datetime
    project_id: str = ""

    @property
    def security_context(self) -> SecurityContext:
        revision = (self.artefact or {}).get("tree_sha256") or self.base_commit
        return SecurityContext(self.destination_repo, self.action_id, revision, self.project_id)

    @property
    def executable_action(self) -> str:
        """The action the task may execute: the verified action, or a recorded override."""
        if self.override:
            return str(self.override["new_action"])
        return self.action


def _row_to_task(row: sqlite3.Row) -> Task:
    return Task(
        task_id=row["task_id"],
        action_id=row["action_id"],
        run_id=row["run_id"],
        generation=row["generation"],
        action=row["action"],
        task_class=row["task_class"],
        task_text=row["task_text"],
        scope=tuple(json.loads(row["scope_json"])),
        destination_repo=row["destination_repo"],
        destination_node_id=row["destination_node_id"],
        base_ref=row["base_ref"],
        base_commit=row["base_commit"],
        publication_ref=row["publication_ref"],
        snapshot_id=row["snapshot_id"],
        policy_version=row["policy_version"],
        state=TaskState(row["state"]),
        support_status=row["support_status"],
        security_decision_id=row["security_decision_id"],
        override=json.loads(row["override_json"]) if row["override_json"] else None,
        artefact=json.loads(row["artefact_json"]) if row["artefact_json"] else None,
        checks=json.loads(row["checks_json"] or "{}"),
        published_commit=row["published_commit"],
        pr_number=row["pr_number"],
        published_at=parse_iso(row["published_at"]),
        defect_window_ends=parse_iso(row["defect_window_ends"]),
        created_at=parse_iso(row["created_at"]),  # type: ignore[arg-type]
        updated_at=parse_iso(row["updated_at"]),  # type: ignore[arg-type]
        project_id=(row["project_id"] if "project_id" in row.keys() else "") or "",
    )


class TaskStore:
    def __init__(self, db: OpsDB) -> None:
        self.db = db

    # -- creation ------------------------------------------------------------------------------

    def create(
        self,
        *,
        run_id: str,
        generation: int,
        action: str,
        task_class: str,
        task_text: str,
        scope: Sequence[str],
        destination_repo: str,
        destination_node_id: str,
        base_ref: str,
        base_commit: str,
        snapshot_id: str,
        policy_version: str,
        scanner: SecurityScanner,
        gate: SecurityGate,
        now: datetime,
        idempotency_key: str | None = None,
        project_id: str = "",
    ) -> Task:
        """Create a task in ``proposed``: allocate the action id, scan the destination at its base
        revision into the action-security partition and record the first gate decision.

        ``project_id`` is the project of the run the task comes from; it is stored with the task and
        every later change is checked against it. Idempotency keys are partitioned by project.
        """
        request = {
            "op": "create_task", "run_id": run_id, "generation": generation, "action": action,
            "task_text": task_text, "scope": list(scope), "destination_repo": destination_repo,
            "base_ref": base_ref, "base_commit": base_commit, "project_id": project_id,
        }
        if idempotency_key is not None and project_id:
            idempotency_key = f"{project_id}::{idempotency_key}"
        with self.db.transaction() as conn:
            stored = self.db.idempotent(conn, idempotency_key, request)
            if stored is not None:
                return self.get(stored["task_id"])
            task_id = new_id("task")
            action_id = new_id("act")
            ctx = SecurityContext(destination_repo, action_id, base_commit, project_id)
            scanner.scan(ctx)
            decision = gate.decide(ctx, now)
            conn.execute(
                "INSERT INTO tasks (task_id, action_id, run_id, generation, action, task_class, task_text,"
                " scope_json, destination_repo, destination_node_id, base_ref, base_commit, publication_ref,"
                " snapshot_id, policy_version, state, security_decision_id, created_at, updated_at, project_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (task_id, action_id, run_id, generation, action, task_class, task_text, json.dumps(list(scope)),
                 destination_repo, destination_node_id, base_ref, base_commit, f"atlas/{action_id}",
                 snapshot_id, policy_version, TaskState.PROPOSED.value, decision.decision_id, iso(now), iso(now),
                 project_id),
            )
            self._history(conn, task_id, None, TaskState.PROPOSED, "created", now)
            self.db.emit_event(conn, "task.state_changed", at=now, run_id=run_id, generation=generation,
                               data={"task_id": task_id, "action_id": action_id, "from": None, "to": "proposed",
                                     "security_state": decision.state.value})
            if decision.state == SecurityState.BLOCKED:
                self._set_state(conn, task_id, TaskState.PROPOSED, TaskState.VOIDED, "initial security scan blocked", now, run_id, generation)
            self.db.remember(conn, idempotency_key, request, {"task_id": task_id})
        return self.get(task_id)

    # -- reads ---------------------------------------------------------------------------------

    def get(self, task_id: str) -> Task:
        row = self.db.one("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
        if row is None:
            raise KeyError(f"unknown task {task_id}")
        return _row_to_task(row)

    def by_action(self, action_id: str) -> Task:
        row = self.db.one("SELECT * FROM tasks WHERE action_id = ?", (action_id,))
        if row is None:
            raise KeyError(f"unknown action {action_id}")
        return _row_to_task(row)

    def for_run(self, run_id: str) -> list[Task]:
        return [_row_to_task(r) for r in self.db.query("SELECT * FROM tasks WHERE run_id = ? ORDER BY created_at", (run_id,))]

    def for_project(self, project_id: str) -> list[Task]:
        return [_row_to_task(r) for r in self.db.query(
            "SELECT * FROM tasks WHERE project_id = ? ORDER BY created_at", (project_id,))]

    def all(self) -> list[Task]:
        return [_row_to_task(r) for r in self.db.query("SELECT * FROM tasks ORDER BY created_at")]

    def history(self, task_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.query("SELECT * FROM task_history WHERE task_id = ? ORDER BY seq", (task_id,))]

    def view(self, task_id: str, *, security_summary: Mapping[str, Any] | None = None,
             validation: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Task representation for the API. Post-publication fields are explicit nulls until a
        publication exists; security appears only as a summary, never raw scan evidence."""
        task = self.get(task_id)
        published = task.state in PUBLISHED_STATES or task.published_commit is not None
        approvals = [
            {"approval_id": r["approval_id"], "kind": r["kind"], "status": r["status"],
             "generation": r["generation"], "expires_at": r["expires_at"]}
            for r in self.db.query("SELECT * FROM approvals WHERE task_id = ? ORDER BY created_at", (task_id,))
        ]
        return {
            "task_id": task.task_id,
            "project_id": task.project_id or None,
            "action_id": task.action_id,
            "run_id": task.run_id,
            "state": task.state.value,
            "support_status": task.support_status,
            "authoritative_generation": task.generation,
            "action": task.action,
            "executable_action": task.executable_action,
            "task_class": task.task_class,
            "task_text": task.task_text,
            "permitted_scope": list(task.scope),
            "destination": {"repository": task.destination_repo, "node_id": task.destination_node_id,
                            "base_ref": task.base_ref, "base_commit": task.base_commit,
                            "publication_ref": task.publication_ref},
            "security": dict(security_summary) if security_summary else {"decision_id": task.security_decision_id},
            "approvals": approvals,
            "preparation_checks": dict(task.checks),
            "artefact": dict(task.artefact) if task.artefact else None,
            "history": self.history(task_id),
            "publication": (
                {"published_commit": task.published_commit, "pr_number": task.pr_number,
                 "published_at": iso(task.published_at) if task.published_at else None,
                 "defect_window_ends": iso(task.defect_window_ends) if task.defect_window_ends else None}
                if published else None
            ),
            "validation": dict(validation) if (published and validation is not None) else None,
        }

    # -- writes --------------------------------------------------------------------------------

    def _history(self, conn: sqlite3.Connection, task_id: str, frm: TaskState | None, to: TaskState, reason: str, now: datetime) -> None:
        conn.execute(
            "INSERT INTO task_history (task_id, from_state, to_state, reason, at) VALUES (?,?,?,?,?)",
            (task_id, frm.value if frm else None, to.value, reason, iso(now)),
        )

    def _set_state(self, conn: sqlite3.Connection, task_id: str, frm: TaskState, to: TaskState, reason: str,
                   now: datetime, run_id: str, generation: int) -> None:
        conn.execute("UPDATE tasks SET state = ?, updated_at = ? WHERE task_id = ?", (to.value, iso(now), task_id))
        self._history(conn, task_id, frm, to, reason, now)
        self.db.emit_event(conn, "task.state_changed", at=now, run_id=run_id, generation=generation,
                           data={"task_id": task_id, "from": frm.value, "to": to.value, "reason": reason})

    def transition(self, task_id: str, to: TaskState, reason: str, now: datetime,
                   *, conn: sqlite3.Connection | None = None, expect: Iterable[TaskState] | None = None) -> Task:
        with self.db.transaction() as c:
            c = conn or c
            row = c.execute("SELECT state, run_id, generation FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            frm = TaskState(row["state"])
            if expect is not None and frm not in set(expect):
                raise TaskError(f"task {task_id} is {frm.value}, expected one of {sorted(s.value for s in expect)}")
            if to not in ALLOWED_TRANSITIONS[frm]:
                raise TaskError(f"task {task_id}: {frm.value} -> {to.value} is not allowed")
            self._set_state(c, task_id, frm, to, reason, now, row["run_id"], row["generation"])
        return self.get(task_id)

    def update_fields(self, task_id: str, now: datetime, *, conn: sqlite3.Connection | None = None, **fields: Any) -> None:
        allowed = {"generation", "security_decision_id", "artefact_json", "checks_json", "published_commit",
                   "pr_number", "published_at", "defect_window_ends", "support_status", "override_json"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"fields not updatable: {sorted(bad)}")
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields) + ", updated_at = ?"
        with self.db.transaction() as c:
            (conn or c).execute(f"UPDATE tasks SET {sets} WHERE task_id = ?", (*fields.values(), iso(now), task_id))

    def record_checks(self, task_id: str, results: Mapping[str, str], now: datetime, *, conn: sqlite3.Connection | None = None) -> None:
        with self.db.transaction() as c:
            c = conn or c
            current = json.loads(c.execute("SELECT checks_json FROM tasks WHERE task_id = ?", (task_id,)).fetchone()[0] or "{}")
            current.update(results)
            self.update_fields(task_id, now, conn=c, checks_json=json.dumps(current, sort_keys=True))

    def record_override(self, task_id: str, *, who: str, reason: str, new_action: str, now: datetime) -> Task:
        """A human override of the verified action. Any existing approval stops matching, so a new
        Approval A is required against the override."""
        task = self.get(task_id)
        if task.state not in UNPUBLISHED_STATES and task.state != TaskState.AWAITING_REAPPROVAL:
            raise TaskError("an override is only possible before publication")
        Action(new_action)
        override = {"who": who, "reason": reason, "original_action": task.action, "new_action": new_action, "at": iso(now)}
        with self.db.transaction() as conn:
            self.update_fields(task_id, now, conn=conn, override_json=json.dumps(override, sort_keys=True))
            conn.execute(
                "UPDATE approvals SET status = 'voided', voided_at = ?, void_reason = ? WHERE task_id = ? AND status = 'granted'",
                (iso(now), "action overridden", task_id),
            )
            if task.state in (TaskState.APPROVED_A, TaskState.PREPARED, TaskState.APPROVED_B):
                self._set_state(conn, task_id, task.state, TaskState.AWAITING_REAPPROVAL, "action overridden", now, task.run_id, task.generation)
        return self.get(task_id)

    def mark_published(self, task_id: str, *, commit: str, pr_number: int, now: datetime, conn: sqlite3.Connection | None = None) -> None:
        with self.db.transaction() as c:
            c = conn or c
            self.update_fields(task_id, now, conn=c, published_commit=commit, pr_number=pr_number,
                               published_at=iso(now), defect_window_ends=iso(now + DEFECT_WINDOW),
                               support_status=SupportStatus.SUPPORTED.value)
            self.transition(task_id, TaskState.PUBLISHED, "publication complete", now, conn=c, expect=[TaskState.APPROVED_B])

    # -- re-verification disposition (by phase) -----------------------------------------------

    def dispose_after_reverification(self, conn: sqlite3.Connection, run_id: str, new_generation: int,
                                     supported_actions: frozenset[str], verification_ok: bool, now: datetime,
                                     journal_complete: "callable") -> list[dict[str, Any]]:
        """Set each dependent task's disposition in the caller's transaction.

        Unpublished: still supported -> ``awaiting_reapproval`` bound to the new generation;
        otherwise ``voided``. Published: state, journal and monitoring kept; ``support_status``
        recorded, and withdrawn support opens a review item. Partially published: publication is
        held for read-only reconciliation; the caller re-runs disposition once reconciled.
        """
        outcomes: list[dict[str, Any]] = []
        rows = conn.execute("SELECT * FROM tasks WHERE run_id = ?", (run_id,)).fetchall()
        for row in rows:
            task = _row_to_task(row)
            if task.generation >= new_generation:
                continue  # already disposed for this generation: idempotent
            supported = verification_ok and task.executable_action in supported_actions
            if task.state in (TaskState.VOIDED,):
                continue
            if task.state in UNPUBLISHED_STATES and not journal_complete(task.action_id) and _journal_started(conn, task.action_id):
                outcomes.append({"task_id": task.task_id, "disposition": "held_for_reconciliation"})
                self.update_fields(task.task_id, now, conn=conn, generation=new_generation)
                continue
            if task.state in UNPUBLISHED_STATES or task.state == TaskState.AWAITING_REAPPROVAL:
                self.update_fields(task.task_id, now, conn=conn, generation=new_generation)
                if supported:
                    if task.state != TaskState.AWAITING_REAPPROVAL and task.state != TaskState.PROPOSED:
                        self._set_state(conn, task.task_id, task.state, TaskState.AWAITING_REAPPROVAL,
                                        f"superseded by generation {new_generation}", now, run_id, new_generation)
                    outcomes.append({"task_id": task.task_id, "disposition": "awaiting_reapproval"
                                     if task.state != TaskState.PROPOSED else "proposed"})
                else:
                    self._set_state(conn, task.task_id, task.state, TaskState.VOIDED,
                                    "support removed or verification failed", now, run_id, new_generation)
                    outcomes.append({"task_id": task.task_id, "disposition": "voided"})
            elif task.state in PUBLISHED_STATES:
                status = SupportStatus.SUPPORTED if supported else SupportStatus.WITHDRAWN
                self.update_fields(task.task_id, now, conn=conn, generation=new_generation, support_status=status.value)
                self.db.emit_event(conn, "task.state_changed", at=now, run_id=run_id, generation=new_generation,
                                   data={"task_id": task.task_id, "from": task.state.value, "to": task.state.value,
                                         "support_status": status.value})
                outcomes.append({"task_id": task.task_id, "disposition": f"published_{status.value}"})
        return outcomes


def _journal_started(conn: sqlite3.Connection, action_id: str) -> bool:
    return conn.execute("SELECT 1 FROM journal WHERE action_id = ? LIMIT 1", (action_id,)).fetchone() is not None
