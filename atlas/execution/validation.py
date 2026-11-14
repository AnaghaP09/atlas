"""Post-publication validation: CI aggregation, behavioural checks, adjudication and acceptance.

Evidence (CI observations, behavioural-check results, adjudications) lives in the action-scoped
validation store and is read through a ``ValidationContext``; it is never task state. One function
applies the evidence to the task store in a single transaction: CI verdict -> awaiting_validation,
all three conditions -> accepted, any failed condition -> failed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from atlas.contracts import PUBLISHED_STATES, TaskState, ValidationContext
from atlas.execution.ports import ValidationEvidence
from atlas.tasks import Task, TaskStore

STATUS_RANK = {"queued": 0, "waiting": 0, "pending": 0, "requested": 0, "in_progress": 1, "completed": 2}
WRITER = "execution"


def observation_identity(obs: Mapping[str, Any]) -> tuple:
    """Identity of one observation: job id, attempt, status, conclusion, started and completed times."""
    return (obs.get("job_id"), obs.get("attempt", 1), obs.get("status"), obs.get("conclusion"),
            obs.get("started_at"), obs.get("completed_at"))


@dataclass
class AttemptState:
    name: str
    job_id: Any
    attempt: int
    status: str
    conclusion: str | None
    conflict: bool = False


def effective_states(observations: Iterable[Mapping[str, Any]], commit: str) -> dict[str, AttemptState]:
    """Latest attempt per check name for the bound commit, each at its effective state."""
    seen: set[tuple] = set()
    attempts: dict[tuple, AttemptState] = {}
    for obs in observations:
        if obs.get("head_sha") != commit:
            continue  # stored, never attributed
        ident = observation_identity(obs)
        if ident in seen:
            continue
        seen.add(ident)
        key = (obs.get("job_id"), obs.get("attempt", 1))
        status = str(obs.get("status") or "queued")
        current = attempts.get(key)
        if current is None:
            attempts[key] = AttemptState(str(obs.get("name")), obs.get("job_id"), int(obs.get("attempt", 1)), status,
                                         obs.get("conclusion") if status == "completed" else None)
            continue
        if current.status == "completed" and status == "completed":
            if obs.get("conclusion") != current.conclusion:
                current.conflict = True
            continue
        if STATUS_RANK.get(status, 0) > STATUS_RANK.get(current.status, 0):
            current.status = status
            current.conclusion = obs.get("conclusion") if status == "completed" else None
    latest: dict[str, AttemptState] = {}
    for st in attempts.values():
        prev = latest.get(st.name)
        if prev is None or st.attempt > prev.attempt:
            latest[st.name] = st
    return latest


def ci_verdict(observations: Iterable[Mapping[str, Any]], commit: str, required: Sequence[str]) -> str:
    states = effective_states(observations, commit)
    verdict = "passed"
    for name in required:
        st = states.get(name)
        if st is None or st.status != "completed" or st.conflict:
            verdict = "pending" if verdict != "failed" else verdict
            continue
        if st.conclusion != "success":
            verdict = "failed"
    return verdict


class ValidationService:
    def __init__(self, tasks: TaskStore, evidence: ValidationEvidence, required_checks: Sequence[str] = ("test",)) -> None:
        self.tasks = tasks
        self.evidence = evidence
        self.required = tuple(required_checks)

    @staticmethod
    def context(task: Task) -> ValidationContext:
        if not task.published_commit:
            raise ValueError("task has no publication")
        return ValidationContext(task.destination_repo, task.action_id, task.published_commit)

    @staticmethod
    def monitoring_allowed(task: Task, now: datetime, repo: str, commit: str) -> bool:
        """Read-only CI monitoring is authorised by the durable publication record, independent of
        Approval B, for published tasks within the defect window."""
        return (task.published_commit is not None and task.state in PUBLISHED_STATES
                and repo.lower() == task.destination_repo.lower() and commit == task.published_commit
                and task.defect_window_ends is not None and now < task.defect_window_ends)

    def record_behavioural(self, task_id: str, *, passed: bool, detail: Mapping[str, Any], now: datetime) -> None:
        task = self.tasks.get(task_id)
        self.evidence.write(self.context(task), "behavioural_check",
                            {"passed": passed, "detail": dict(detail), "at": now.isoformat()}, WRITER)

    def record_adjudication(self, task_id: str, *, adjudicator: str, correct: bool, reason: str, now: datetime,
                            approver_b: str | None = None) -> None:
        task = self.tasks.get(task_id)
        if not reason.strip():
            raise ValueError("an adjudication needs a reason")
        if approver_b and adjudicator == approver_b:
            raise ValueError("the adjudicator must not be the approver of Approval B")
        self.evidence.write(self.context(task), "adjudication",
                            {"adjudicator": adjudicator, "correct": correct, "reason": reason, "at": now.isoformat()}, WRITER)

    def summary(self, task_id: str) -> dict[str, Any]:
        task = self.tasks.get(task_id)
        ctx = self.context(task)
        obs = [r.data for r in self.evidence.read(ctx, "ci_observation")]
        beh = [r.data for r in self.evidence.read(ctx, "behavioural_check")]
        adj = [r.data for r in self.evidence.read(ctx, "adjudication")]
        return {
            "ci_verdict": ci_verdict(obs, task.published_commit, self.required),
            "behavioural": None if not beh else ("passed" if beh[-1]["passed"] else "failed"),
            "adjudication": None if not adj else ("correct" if adj[-1]["correct"] else "incorrect"),
        }

    def apply(self, task_id: str, now: datetime) -> Task:
        """Apply the evidence to the task store in one transaction."""
        with self.tasks.db.transaction() as conn:
            task = self.tasks.get(task_id)
            if task.state not in (TaskState.PUBLISHED, TaskState.AWAITING_VALIDATION):
                return task
            s = self.summary(task_id)
            if task.state == TaskState.PUBLISHED:
                if s["ci_verdict"] == "failed":
                    return self.tasks.transition(task_id, TaskState.FAILED, "CI failed", now, conn=conn)
                if s["ci_verdict"] != "passed":
                    return task
                self.tasks.transition(task_id, TaskState.AWAITING_VALIDATION, "CI passed", now, conn=conn)
            if s["ci_verdict"] == "failed" or s["behavioural"] == "failed" or s["adjudication"] == "incorrect":
                return self.tasks.transition(task_id, TaskState.FAILED, f"validation failed: {s}", now, conn=conn)
            if s["ci_verdict"] == "passed" and s["behavioural"] == "passed" and s["adjudication"] == "correct":
                return self.tasks.transition(task_id, TaskState.ACCEPTED, "CI, behavioural check and adjudication passed",
                                             now, conn=conn)
        return self.tasks.get(task_id)
