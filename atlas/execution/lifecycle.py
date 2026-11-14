"""Lifecycle operations that cut across approvals and tasks: security refresh and re-verification."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from atlas.approvals import ApprovalService
from atlas.contracts import Role, SecurityContext, SecurityState, TaskState
from atlas.execution.ports import SecurityGate, SecurityScanner
from atlas.tasks import TaskStore

REFRESHABLE = (TaskState.PROPOSED, TaskState.APPROVED_A, TaskState.PREPARED, TaskState.APPROVED_B,
               TaskState.AWAITING_REAPPROVAL)


class RefreshError(ValueError):
    pass


def security_refresh(task_id: str, *, role: str, tasks: TaskStore, approvals: ApprovalService,
                     scanner: SecurityScanner, gate: SecurityGate, now: datetime,
                     idempotency_key: str | None = None) -> dict[str, Any]:
    """Renew a task's security decision.

    In ``proposed``: the task keeps its action id and stays ``proposed`` bound to the new
    decision (a blocked result voids it). Later: both approvals are voided (never rewritten), the
    task is bound to the new decision and moves to ``awaiting_reapproval`` (``voided`` if blocked).
    """
    if role != Role.EM.value:
        raise RefreshError("security refresh is started by the engineering manager role")
    db = tasks.db
    request = {"op": "security_refresh", "task_id": task_id}
    with db.transaction() as conn:
        stored = db.idempotent(conn, idempotency_key, request)
        if stored is not None:
            return stored
        task = tasks.get(task_id)
        if task.state not in REFRESHABLE:
            raise RefreshError(f"security refresh is not allowed in state {task.state.value}")
        revision = (task.artefact or {}).get("tree_sha256") or task.base_commit
        ctx = SecurityContext(task.destination_repo, task.action_id, revision)
        scanner.scan(ctx)
        decision = gate.decide(ctx, now)
        tasks.update_fields(task_id, now, conn=conn, security_decision_id=decision.decision_id)
        voided: list[str] = []
        if task.state == TaskState.PROPOSED:
            new_state = TaskState.VOIDED if decision.state == SecurityState.BLOCKED else TaskState.PROPOSED
        else:
            voided = approvals.void_task(task_id, "security decision renewed", now, conn=conn)
            new_state = TaskState.VOIDED if decision.state == SecurityState.BLOCKED else TaskState.AWAITING_REAPPROVAL
        if new_state != task.state:
            tasks.transition(task_id, new_state, f"security refresh -> {decision.state.value}", now, conn=conn)
        outcome = {"task_id": task_id, "decision_id": decision.decision_id, "security_state": decision.state.value,
                   "state": new_state.value, "voided_approvals": voided}
        db.remember(conn, idempotency_key, request, outcome)
    return outcome


def apply_reverification(*, run_id: str, new_generation: int, supported_actions: frozenset[str],
                         verification_ok: bool, tasks: TaskStore, approvals: ApprovalService,
                         journal_complete: Callable[[str], bool], now: datetime, conn=None,
                         notifier: Any = None) -> dict[str, Any]:
    """The execution side of a re-verification, inside the caller's transaction: void every
    approval bound to a lower generation, then set each dependent task's disposition by phase."""
    with tasks.db.transaction() as c:
        c = conn or c
        voided = approvals.void_superseded(run_id, new_generation, conn=c, now=now)
        outcomes = tasks.dispose_after_reverification(c, run_id, new_generation, supported_actions,
                                                      verification_ok, now, journal_complete)
    if notifier is not None:
        for o in outcomes:
            if o["disposition"] == "published_withdrawn":
                notifier.emit("task.support_withdrawn", o["task_id"], [o["task_id"]], frozenset({"public"}))
            elif o["disposition"] == "voided":
                notifier.emit("task.voided", o["task_id"], [o["task_id"]], frozenset({"public"}))
    return {"voided_approvals": voided, "tasks": outcomes}
