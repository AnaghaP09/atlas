"""Re-verification across the agents layer and the execution layer, using both real implementations.

The run orchestrator writes a new verification generation and calls the approval service's
``void_superseded``; the execution layer then applies each task's disposition by publication phase.
"""

from __future__ import annotations

from typing import Any, Mapping

from atlas.contracts import REVERIFICATION_ASSESSMENTS, SupportStatus, TaskDisposition, TaskState
from atlas.execution.lifecycle import apply_reverification
from tests.b.test_orchestrator import ONCALL, TRIGGER, build, finish_dependency, recs, verdicts
from tests.b.test_agents import step_tool
from tests.b.helpers import ScriptedOutbound
from tests.c.fakes import make_env

RUN = "run-x"


class ExecutionDisposition:
    """Maps the orchestrator's per-task assessments onto the execution layer's re-verification."""

    def __init__(self, env: Any) -> None:
        self.env = env

    def tasks_for_run(self, run_id: str) -> list[Mapping[str, Any]]:
        return [{"task_id": t.task_id, "action": t.executable_action, "state": t.state.value}
                for t in self.env.tasks.for_run(run_id)]

    def apply_reverification(self, run_id: str, new_generation: int, assessments: Mapping[str, str]) -> None:
        assert set(assessments.values()) <= set(REVERIFICATION_ASSESSMENTS)
        tasks = {t["task_id"]: t for t in self.tasks_for_run(run_id)}
        supported = frozenset(tasks[tid]["action"] for tid, a in assessments.items() if a == "still_supported")
        ok = "verification_failed" not in assessments.values()
        self.env.run_state.generation[run_id] = new_generation
        apply_reverification(run_id=run_id, new_generation=new_generation, supported_actions=supported,
                             verification_ok=ok, tasks=self.env.tasks, approvals=self.env.approvals,
                             journal_complete=self.env.publisher.complete, now=self.env.now,
                             notifier=self.env.notifier)


def _setup(run_context, verifier_script):
    env = make_env()
    disposition = ExecutionDisposition(env)
    assert isinstance(disposition, TaskDisposition)
    scripted = ScriptedOutbound({
        "investigator": [step_tool("get_issue", issue_id="18234"), step_tool("get_failed_pipeline_jobs", test_id=""),
                         finish_dependency()],
        "verifier": verifier_script,
    })
    orch, deps = build(run_context, scripted, records=recs(run_context), approvals=env.approvals, tasks=disposition)
    orch.run(RUN, run_context, TRIGGER, ONCALL)
    unpublished = env.to_approved_b(run_id=RUN)
    published = env.to_published(run_id=RUN, key="second-task")
    return env, orch, unpublished, published


def _approval_rows(env, task_id):
    return env.approvals.rows(task_id)


def test_still_supported_unpublished_awaits_reapproval_published_keeps_state(run_context) -> None:
    env, orch, unpublished, published = _setup(
        run_context, [verdicts(c1="supported", c2="supported"), verdicts(c1="supported", c2="supported")])
    assert all(r["status"] == "granted" for r in _approval_rows(env, unpublished.task_id))

    out = orch.reverify(RUN)
    assert out["authoritative_generation"] == 2

    u = env.tasks.get(unpublished.task_id)
    assert u.state == TaskState.AWAITING_REAPPROVAL and u.generation == 2
    assert all(r["status"] == "voided" for r in _approval_rows(env, unpublished.task_id))

    p = env.tasks.get(published.task_id)
    assert p.state in (TaskState.PUBLISHED, TaskState.AWAITING_VALIDATION)
    assert p.support_status == SupportStatus.SUPPORTED.value
    assert env.db.events("approval.voided")


def test_unsupported_voids_unpublished_and_withdraws_support_from_published(run_context) -> None:
    env, orch, unpublished, published = _setup(
        run_context, [verdicts(c1="supported", c2="supported"), verdicts(c1="rejected", c2="supported")])

    out = orch.reverify(RUN)
    assert out["action"]["value"] is None

    assert env.tasks.get(unpublished.task_id).state == TaskState.VOIDED
    assert all(r["status"] == "voided" for r in _approval_rows(env, unpublished.task_id))
    p = env.tasks.get(published.task_id)
    assert p.state in (TaskState.PUBLISHED, TaskState.AWAITING_VALIDATION)
    assert p.support_status == SupportStatus.WITHDRAWN.value
    sent = [n[0] for n in env.notifier.sent] if hasattr(env.notifier, "sent") else []
    assert not sent or "task.voided" in sent and "task.support_withdrawn" in sent


def test_failed_verification_voids_unpublished(run_context) -> None:
    env, orch, unpublished, published = _setup(
        run_context, [verdicts(c1="supported", c2="supported"), RuntimeError("timeout")])

    orch.reverify(RUN)
    assert env.tasks.get(unpublished.task_id).state == TaskState.VOIDED
    assert env.tasks.get(published.task_id).support_status == SupportStatus.WITHDRAWN.value
