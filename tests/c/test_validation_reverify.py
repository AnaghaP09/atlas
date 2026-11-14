import itertools
from datetime import timedelta

import pytest

from atlas.contracts import TaskState
from atlas.execution.lifecycle import apply_reverification
from atlas.execution.validation import ValidationService, ci_verdict, effective_states
from atlas.scorecard import BaselineTask, Scorecard, fully_loaded_cost
from tests.c.fakes import REPO, make_env

COMMIT = "c" * 40


def job(status, conclusion=None, *, job_id=1, attempt=1, name="test", head=COMMIT, started="2026-07-30T12:00:00Z",
        completed=None):
    # shaped like recorded job payloads: no updated_at field
    return {"job_id": job_id, "attempt": attempt, "name": name, "status": status, "conclusion": conclusion,
            "head_sha": head, "started_at": started, "completed_at": completed}


def test_pending_to_completed_within_one_attempt():
    obs = [job("queued", started=None), job("in_progress"), job("completed", "success", completed="2026-07-30T12:05:00Z")]
    assert ci_verdict(obs, COMMIT, ["test"]) == "passed"
    assert ci_verdict(obs[:2], COMMIT, ["test"]) == "pending"


def test_out_of_order_and_duplicates_keep_highest_rank():
    done = job("completed", "success", completed="t1")
    for order in itertools.permutations([job("queued", started=None), job("in_progress"), done, dict(done)]):
        assert ci_verdict(order, COMMIT, ["test"]) == "passed"


def test_conflicting_completed_conclusions_stay_pending():
    obs = [job("completed", "success", completed="t1"), job("completed", "failure", completed="t2")]
    assert ci_verdict(obs, COMMIT, ["test"]) == "pending"
    assert effective_states(obs, COMMIT)["test"].conflict


def test_rerun_is_new_record_and_latest_attempt_counts():
    obs = [job("completed", "failure", completed="t1"), job("completed", "success", attempt=2, completed="t2")]
    assert ci_verdict(obs, COMMIT, ["test"]) == "passed"


def test_mixed_required_outcomes_any_order_and_other_commit_ignored():
    a_fail = job("completed", "failure", name="test", completed="t")
    b_ok = job("completed", "success", job_id=2, name="lint", completed="t")
    other = job("completed", "success", job_id=3, name="test", head="d" * 40, completed="t")
    for order in itertools.permutations([a_fail, b_ok, other]):
        assert ci_verdict(order, COMMIT, ["test", "lint"]) == "failed"
    assert ci_verdict([other], COMMIT, ["test"]) == "pending"


def _published():
    env = make_env()
    task = env.to_published()
    return env, task


def _ci(env, task, conclusion="success"):
    ctx = ValidationService.context(task)
    env.evidence.write(ctx, "ci_observation", job("completed", conclusion, head=task.published_commit, completed="t"), "target-ci")


def test_acceptance_needs_ci_behavioural_and_adjudication():
    env, task = _published()
    assert env.validation.apply(task.task_id, env.now).state == TaskState.PUBLISHED
    _ci(env, task)
    assert env.validation.apply(task.task_id, env.now).state == TaskState.AWAITING_VALIDATION
    env.validation.record_behavioural(task.task_id, passed=True, detail={"test": "TestJoinHostPortIPv6"}, now=env.now)
    assert env.validation.apply(task.task_id, env.now).state == TaskState.AWAITING_VALIDATION
    with pytest.raises(ValueError):
        env.validation.record_adjudication(task.task_id, adjudicator="ana", correct=True, reason="ok", now=env.now,
                                           approver_b="ana")
    env.validation.record_adjudication(task.task_id, adjudicator="swe", correct=True, reason="fix is correct", now=env.now,
                                       approver_b="ana")
    assert env.validation.apply(task.task_id, env.now).state == TaskState.ACCEPTED
    view = env.tasks.view(task.task_id, validation=env.validation.summary(task.task_id))
    assert view["state"] == "accepted" and view["validation"]["ci_verdict"] == "passed"


def test_failed_condition_fails_task():
    env, task = _published()
    _ci(env, task, "failure")
    assert env.validation.apply(task.task_id, env.now).state == TaskState.FAILED


def test_state_agrees_after_restart(tmp_path):
    from tests.c.fakes import make_env as mk

    env = mk(db_path=tmp_path / "ops.sqlite")
    task = env.to_published()
    _ci(env, task)
    env.validation.apply(task.task_id, env.now)
    env.db.close()
    env2 = mk(db_path=tmp_path / "ops.sqlite")
    t = env2.tasks.get(task.task_id)
    assert t.state == TaskState.AWAITING_VALIDATION
    assert env2.tasks.view(task.task_id)["state"] == "awaiting_validation"


def test_monitoring_authorised_by_publication_record_not_approval():
    env, task = _published()
    env.clock.advance(hours=72)  # Approval B long expired
    assert ValidationService.monitoring_allowed(env.tasks.get(task.task_id), env.now, REPO, task.published_commit)
    assert not ValidationService.monitoring_allowed(env.tasks.get(task.task_id), env.now, "other/repo", task.published_commit)
    assert not ValidationService.monitoring_allowed(env.tasks.get(task.task_id), env.now, REPO, "e" * 40)
    env.clock.advance(days=31)
    assert not ValidationService.monitoring_allowed(env.tasks.get(task.task_id), env.now, REPO, task.published_commit)


# -- re-verification by phase (shared with the agents' caller through apply_reverification) ------

SUPPORTED = frozenset({"DELEGATE_TO_CODING_AGENT"})


def _reverify(env, supported=SUPPORTED, ok=True, gen=2):
    env.run_state.generation["run-1"] = gen
    return apply_reverification(run_id="run-1", new_generation=gen, supported_actions=supported, verification_ok=ok,
                                tasks=env.tasks, approvals=env.approvals, journal_complete=env.publisher.complete,
                                now=env.now, notifier=env.notifier)


def test_unpublished_supported_goes_to_awaiting_reapproval_at_new_generation():
    env = make_env()
    task = env.to_approved_b()
    out = _reverify(env)
    t = env.tasks.get(task.task_id)
    assert t.state == TaskState.AWAITING_REAPPROVAL and t.generation == 2
    assert len(out["voided_approvals"]) == 2
    assert all(r["status"] == "voided" for r in env.approvals.rows(task.task_id))
    assert len(env.db.events("approval.voided")) == 2
    # repeat is a no-op
    again = _reverify(env)
    assert again["voided_approvals"] == [] and again["tasks"] == []


@pytest.mark.parametrize("supported,ok", [(frozenset(), True), (SUPPORTED, False)])
def test_unpublished_unsupported_or_failed_is_voided(supported, ok):
    env = make_env()
    task = env.to_prepared()
    _reverify(env, supported, ok)
    assert env.tasks.get(task.task_id).state == TaskState.VOIDED


@pytest.mark.parametrize("accepted", [False, True])
@pytest.mark.parametrize("supported", [True, False])
def test_published_keeps_state_and_monitoring(accepted, supported, tmp_path):
    from tests.c.fakes import make_env as mk

    env = mk(db_path=tmp_path / "ops.sqlite")
    task = env.to_published()
    if accepted:
        _ci(env, task)
        env.validation.record_behavioural(task.task_id, passed=True, detail={}, now=env.now)
        env.validation.record_adjudication(task.task_id, adjudicator="swe", correct=True, reason="ok", now=env.now)
        env.validation.apply(task.task_id, env.now)
    before = env.tasks.get(task.task_id).state
    _reverify(env, SUPPORTED if supported else frozenset())
    env.db.close()
    env2 = mk(db_path=tmp_path / "ops.sqlite")
    t = env2.tasks.get(task.task_id)
    assert t.state == before
    assert t.support_status == ("supported" if supported else "withdrawn")
    assert ValidationService.monitoring_allowed(t, env.now, REPO, t.published_commit)
    if not supported:
        assert "task.support_withdrawn" in [n["event_type"] for n in env.notifier.records]
    # a later CI observation is still ingested
    env2.evidence = env.evidence
    env2.validation.evidence = env.evidence
    _ci(env2, t)
    assert env2.validation.summary(task.task_id)["ci_verdict"] == "passed"


def test_partially_published_action_is_held_for_reconciliation():
    from tests.c.fakes import LostResponse

    env = make_env()
    task = env.to_approved_b()
    env.api.lose_after.add("create_pull")
    with pytest.raises(LostResponse):
        env.publisher.publish(task.task_id, env.now)
    out = _reverify(env)
    assert out["tasks"][0]["disposition"] == "held_for_reconciliation"


# -- scorecard ---------------------------------------------------------------------------------


def test_scorecard_versioning_latency_vs_effort_and_unavailable_rule():
    env = make_env()
    sc = Scorecard(env.db, env.notifier)
    data = sc.row_data(accepted=True, ci="passed", behavioural="passed", review_minutes=12, approval_latency_minutes=600,
                       revisions=1, corrective_tasks=0,
                       cost_usd=fully_loaded_cost(model_usd=0.4, sandbox_minutes=2, ci_minutes=3, review_minutes=12),
                       defect_window="open")
    assert data["review_minutes"] == 12 and data["approval_latency_minutes"] == 600 and data["rework"] == 1
    assert sc.write_row("t1", "test_adjustment", data, env.now) == 1
    assert sc.write_row("t1", "test_adjustment", {**data, "defect_window": "closed"}, env.now) == 2
    assert sc.latest_rows()[0]["version"] == 2
    synthetic = [BaselineTask("test_adjustment", True, 30, 0, 60.0)] * 5
    result = sc.compare("test_adjustment", synthetic, env.now)
    assert result["net_contribution"] == "unavailable (no valid baseline)"
    assert result["illustrative_comparison"]["data_mode"] == "SYNTHETIC"
    real = [BaselineTask("test_adjustment", True, 5, 0, 10.0, synthetic=False)] * 3
    negative = sc.compare("test_adjustment", real, env.now)
    assert "review_minutes" in negative["negative_metrics"]
    assert "scorecard.class_negative" in [n["event_type"] for n in env.notifier.records]
    assert sc.compare("test_adjustment", real[:2], env.now)["net_contribution"] == "unavailable (no valid baseline)"
