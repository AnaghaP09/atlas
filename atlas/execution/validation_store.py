"""Validation evidence kept in the data layer's action store, and the monitor that feeds it.

``StoreValidationEvidence`` is the execution layer's view of the action-scoped validation store:
CI observations (written only by the target-CI adapter), behavioural-check results and human
adjudications (written by the execution layer). It holds evidence only; a task's state lives in
the task store.

``TargetMonitor`` follows published tasks. It asks the target-CI adapter for new observations of
the published commit, records the behavioural check once the CI job that runs it has finished,
applies the evidence to the task and writes the task's scorecard row. Monitoring is authorised by
the durable publication record, so it keeps working after an approval has expired.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from atlas.contracts import PUBLISHED_STATES, EvidenceRecord, TaskState, ValidationContext
from atlas.execution.validation import ValidationService, ci_verdict
from atlas.tasks import TaskStore

READER = "execution"
WRITER = "execution"
# The execution layer's names for evidence kinds -> the store's names.
KIND_IN_STORE = {"behavioural_check": "behavioural_check", "adjudication": "human_adjudication"}


def _when(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return datetime.now(timezone.utc)


class StoreValidationEvidence:
    """``ValidationEvidence`` over the action store (never memory, never a snapshot)."""

    def __init__(self, action_store: Any, clock: Callable[[], datetime] | None = None) -> None:
        self.store = action_store
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def read(self, ctx: ValidationContext, kind: str) -> list[EvidenceRecord]:
        if kind == "ci_observation":
            out = []
            for row in self.store.ci_observations(ctx, READER):
                data = {"job_id": row["job_id"], "attempt": row["run_attempt"], "name": row["name"],
                        "status": row["status"], "conclusion": row["conclusion"], "started_at": row["started_at"],
                        "completed_at": row["completed_at"],
                        # Observations are stored under the published commit they belong to.
                        "head_sha": ctx.published_commit}
                out.append(EvidenceRecord("ci_observation", data, _when(row["observed_at"])))
            return out
        if kind not in KIND_IN_STORE:
            raise ValueError(f"unknown validation evidence kind {kind}")
        return [EvidenceRecord(kind, row["content"], _when(row["recorded_at"]))
                for row in self.store.evidence(ctx, READER, KIND_IN_STORE[kind])]

    def write(self, ctx: ValidationContext, kind: str, data: Mapping[str, Any], writer: str) -> None:
        if kind == "ci_observation":
            raise PermissionError("CI observations are written only by the target CI adapter")
        if kind not in KIND_IN_STORE:
            raise ValueError(f"unknown validation evidence kind {kind}")
        self.store.write_evidence(ctx, WRITER, KIND_IN_STORE[kind], dict(data), self.clock())


class TargetMonitor:
    """Polls the target's CI for every published task and applies what it finds."""

    def __init__(self, tasks: TaskStore, validation: ValidationService, adapter: Any, record_for: Callable[[Any], Any],
                 scorecard: Any = None, approvals: Any = None, clock: Callable[[], datetime] | None = None,
                 behaviour_job: str = "test") -> None:
        self.tasks = tasks
        self.validation = validation
        self.adapter = adapter
        self.record_for = record_for
        self.scorecard = scorecard
        self.approvals = approvals
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        # The CI job that also runs the behavioural tests written before the task.
        self.behaviour_job = behaviour_job
        self.last_error: str | None = None

    def watched(self) -> list[Any]:
        now = self.clock()
        return [t for t in self.tasks.all()
                if t.published_commit and t.state in PUBLISHED_STATES
                and t.defect_window_ends is not None and now < t.defect_window_ends]

    def poll_task(self, task_id: str) -> dict[str, Any]:
        task = self.tasks.get(task_id)
        now = self.clock()
        new = self.adapter.poll(self.record_for(task))
        ctx = self.validation.context(task)
        observations = [r.data for r in self.validation.evidence.read(ctx, "ci_observation")]
        job = ci_verdict(observations, task.published_commit, (self.behaviour_job,))
        recorded = self.validation.evidence.read(ctx, "behavioural_check")
        if job in ("passed", "failed") and not recorded:
            self.validation.record_behavioural(
                task_id, passed=(job == "passed"), now=now,
                detail={"source": f"target CI job '{self.behaviour_job}', which runs the behavioural tests"})
        before = task.state
        after = self.validation.apply(task_id, now).state
        if after != before or new:
            self.write_scorecard(task_id, now)
        return {"task_id": task_id, "new_observations": new, "state": after.value}

    def poll_all(self) -> list[dict[str, Any]]:
        results = []
        self.last_error = None
        for task in self.watched():
            try:
                results.append(self.poll_task(task.task_id))
            except Exception as exc:  # noqa: BLE001 - one task must not stop the others
                self.last_error = f"{task.task_id}: {exc}"
                results.append({"task_id": task.task_id, "error": str(exc)})
        return results

    def write_scorecard(self, task_id: str, now: datetime) -> None:
        """One scorecard row per task, rewritten as a new version when its evidence changes."""
        if self.scorecard is None:
            return
        task = self.tasks.get(task_id)
        summary = self.validation.summary(task_id)
        latency = 0.0
        if self.approvals is not None:
            stamps = sorted(str(r["created_at"]) for r in self.approvals.rows(task_id))
            if len(stamps) >= 2:
                latency = max(0.0, (_when(stamps[-1]) - _when(stamps[0])).total_seconds() / 60.0)
        window = "closed" if task.defect_window_ends and now >= task.defect_window_ends else "open"
        row = self.scorecard.row_data(
            accepted=task.state == TaskState.ACCEPTED, ci=summary["ci_verdict"],
            behavioural=summary["behavioural"] or "pending", review_minutes=0.0,
            approval_latency_minutes=round(latency, 1), revisions=0, corrective_tasks=0, cost_usd=0.0,
            defect_window=window)
        row["state"] = task.state.value
        row["adjudication"] = summary["adjudication"] or "pending"
        # Review effort and model cost are not measured per task in this build; the row says so.
        row["not_measured"] = ["review_minutes", "cost_usd"]
        self.scorecard.write_row(task_id, task.task_class, row, now)


def required_checks(names: Sequence[str] | None) -> tuple[str, ...]:
    return tuple(names) if names else ("test",)
