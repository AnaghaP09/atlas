from datetime import timedelta

import pytest

from atlas.approvals import ApprovalError
from atlas.containment import PREPARATION
from atlas.contracts import Action, ApprovalKind, SecurityState, TaskState
from atlas.eligibility import APPROVAL_B_CHECKS, MANIFEST
from atlas.execution.lifecycle import RefreshError, security_refresh
from tests.c.fakes import REPO, make_env

NON_EXECUTABLE = [a.value for a in Action if a != Action.DELEGATE_TO_CODING_AGENT]


def test_create_from_empty_store_allocates_action_and_first_decision():
    env = make_env()
    task = env.new_task()
    assert task.state == TaskState.PROPOSED
    assert task.action_id.startswith("act-") and task.publication_ref == f"atlas/{task.action_id}"
    assert env.scanner.scans[0].action_id == task.action_id and env.scanner.scans[0].scanned_revision == "base000"
    assert task.security_decision_id == "sec-1"
    view = env.tasks.view(task.task_id)
    assert view["publication"] is None and view["validation"] is None
    assert view["security"] == {"decision_id": "sec-1"}
    assert [e["data"]["to"] for e in env.db.events("task.state_changed")] == ["proposed"]


def test_create_is_idempotent_with_key():
    env = make_env()
    a = env.new_task(key="k1")
    b = env.new_task(key="k1")
    assert a.task_id == b.task_id and len(env.tasks.all()) == 1


def test_blocked_initial_scan_voids_task():
    env = make_env()
    env.gate.next_state = SecurityState.BLOCKED
    assert env.new_task().state == TaskState.VOIDED


def test_full_path_to_prepared_then_approval_b():
    env = make_env()
    task = env.to_approved_b()
    assert task.state == TaskState.APPROVED_B
    assert task.artefact["diff_sha256"] and task.artefact["tree_sha256"]
    for state in ("proposed", "approved_a", "prepared", "approved_b"):
        assert state in [h["to_state"] for h in env.tasks.history(task.task_id)]


@pytest.mark.parametrize("action", NON_EXECUTABLE)
def test_non_executable_actions_refused(action):
    env = make_env()
    task = env.new_task(action=action)
    with pytest.raises(ApprovalError, match="not executable"):
        env.approvals.grant_a(task.task_id, role="em", eligibility=env.eligibility, now=env.now)


def test_override_requires_new_approval_a():
    env = make_env()
    task = env.new_task(action=Action.ASSIGN_DEVELOPER.value)
    env.tasks.record_override(task.task_id, who="em-1", reason="trivial change", new_action="DELEGATE_TO_CODING_AGENT",
                              now=env.now)
    env.approvals.grant_a(task.task_id, role="em", eligibility=env.eligibility, now=env.now)
    assert env.tasks.get(task.task_id).state == TaskState.APPROVED_A
    # an override after approval voids it and needs a new Approval A
    env.tasks.record_override(task.task_id, who="em-1", reason="changed mind", new_action="DELEGATE_TO_CODING_AGENT",
                              now=env.now)
    assert env.tasks.get(task.task_id).state == TaskState.AWAITING_REAPPROVAL
    assert env.approvals.current(task.task_id, ApprovalKind.A) is None


def test_only_em_role_approves():
    env = make_env()
    task = env.new_task()
    with pytest.raises(ApprovalError):
        env.approvals.grant_a(task.task_id, role="pm", eligibility=env.eligibility, now=env.now)


def test_approval_b_refused_while_any_manifest_check_not_passed():
    for check in APPROVAL_B_CHECKS:
        for status in ("failed", "unavailable", "missing"):
            env = make_env()
            task = env.to_prepared()
            checks = dict(env.tasks.get(task.task_id).checks)
            if status == "missing":
                checks.pop(check, None)
            else:
                checks[check] = status
            import json

            env.tasks.update_fields(task.task_id, env.now, checks_json=json.dumps(checks))
            with pytest.raises(ApprovalError, match=f"check {check}"):
                env.approvals.grant_b(task.task_id, role="em", eligibility=env.eligibility, now=env.now)


def test_manifest_is_versioned_and_read_by_predicate():
    assert set(APPROVAL_B_CHECKS) <= set(MANIFEST["publication_step"]["checks"])
    assert "outgoing_commit_match" in MANIFEST["publication_step"]["checks"]


def test_security_expiry_exception_revocation_and_policy_change_fail_closed():
    env = make_env()
    task = env.to_prepared()
    env.clock.advance(hours=25)
    with pytest.raises(ApprovalError, match="security decision"):
        env.approvals.grant_b(task.task_id, role="em", eligibility=env.eligibility, now=env.now)
    env2 = make_env()
    t2 = env2.to_prepared()
    env2.gate.revoked.add(t2.security_decision_id)
    assert not env2.eligibility.evaluate(t2.task_id, "approval_b", env2.now).allowed
    env3 = make_env()
    t3 = env3.to_prepared()
    env3.gate.policy_version = "policy-2"
    assert "security policy version changed" in env3.eligibility.evaluate(t3.task_id, "approval_b", env3.now).reasons


def test_injection_and_containment_block():
    env = make_env()
    task = env.new_task()
    env.run_state.injection.add("run-1")
    assert not env.eligibility.evaluate(task.task_id, "approval_a", env.now).allowed
    env.run_state.injection.clear()
    env.containment.trip(7, producer="eligibility", detail={}, now=env.now)
    assert any("containment" in r for r in env.eligibility.evaluate(task.task_id, "approval_a", env.now).reasons)
    assert not env.containment.permits(PREPARATION).allowed


def test_void_superseded_only_lower_generations_and_idempotent():
    env = make_env()
    t1 = env.new_task()
    env.approvals.grant_a(t1.task_id, role="em", eligibility=env.eligibility, now=env.now)
    voided = env.approvals.void_superseded("run-1", 2, now=env.now)
    assert len(voided) == 1
    assert env.approvals.void_superseded("run-1", 2, now=env.now) == []
    assert len(env.db.events("approval.voided")) == 1
    assert env.tasks.get(t1.task_id).state == TaskState.APPROVED_A  # task state untouched by the helper
    assert env.approvals.void_superseded("run-1", 1, now=env.now) == []


def test_destination_mismatch_voids_both_and_trips_row_1():
    env = make_env()
    task = env.to_approved_b()
    problems = env.approvals.verify_destination(task, "R_other", "someone/else", task.publication_ref, env.now)
    assert problems
    assert all(r["status"] == "voided" for r in env.approvals.rows(task.task_id))
    assert env.containment.open_incidents()[0]["row"] == 1


def test_destination_switch_across_restart_voids(tmp_path):
    from tests.c.fakes import make_env as mk

    env = mk(db_path=tmp_path / "ops.sqlite")
    task = env.to_approved_b()
    env.db.close()
    env2 = mk(db_path=tmp_path / "ops.sqlite")
    env2.api.identities[REPO] = env2.api.identities[REPO].__class__("R_swapped", REPO)
    env2.gate.decisions.update(env.gate.decisions)
    from atlas.execution.publication import PublicationRefused

    with pytest.raises(PublicationRefused):
        env2.publisher.publish(task.task_id, env2.now)
    assert all(r["status"] == "voided" for r in env2.approvals.rows(task.task_id))


def test_sweep_reminders_and_expiry():
    env = make_env()
    task = env.new_task()
    env.clock.advance(hours=5)
    assert task.task_id in env.approvals.sweep(env.now)["sla_missed"]
    env.approvals.grant_a(task.task_id, role="em", eligibility=env.eligibility, now=env.now)
    env.clock.advance(hours=47, minutes=30)
    assert env.approvals.sweep(env.now)["nearing_expiry"]
    env.clock.advance(hours=1)
    assert env.approvals.sweep(env.now)["expired"]
    kinds = [n["event_type"] for n in env.notifier.records]
    assert {"approval.sla_missed", "approval.nearing_expiry", "approval.expired"} <= set(kinds)


def test_refresh_in_proposed_keeps_state_and_action_id():
    env = make_env()
    task = env.new_task()
    env.clock.advance(hours=25)
    with pytest.raises(ApprovalError):
        env.approvals.grant_a(task.task_id, role="em", eligibility=env.eligibility, now=env.now)
    out = security_refresh(task.task_id, role="em", tasks=env.tasks, approvals=env.approvals, scanner=env.scanner,
                           gate=env.gate, now=env.now)
    t = env.tasks.get(task.task_id)
    assert out["state"] == "proposed" and t.action_id == task.action_id and t.security_decision_id == "sec-2"
    env.approvals.grant_a(task.task_id, role="em", eligibility=env.eligibility, now=env.now)


def test_refresh_initial_failure_then_recovery():
    env = make_env()
    env.gate.next_state = SecurityState.NOT_RUN
    task = env.new_task()
    assert task.state == TaskState.PROPOSED
    assert not env.eligibility.evaluate(task.task_id, "approval_a", env.now).allowed
    env.gate.next_state = SecurityState.PASSED
    security_refresh(task.task_id, role="em", tasks=env.tasks, approvals=env.approvals, scanner=env.scanner,
                     gate=env.gate, now=env.now)
    assert env.eligibility.evaluate(task.task_id, "approval_a", env.now).allowed


def test_refresh_after_approval_voids_both_and_needs_new_a_then_b():
    env = make_env()
    task = env.to_approved_b()
    old = env.approvals.rows(task.task_id)
    out = security_refresh(task.task_id, role="em", tasks=env.tasks, approvals=env.approvals, scanner=env.scanner,
                           gate=env.gate, now=env.now)
    assert out["state"] == "awaiting_reapproval" and len(out["voided_approvals"]) == 2
    assert env.gate.contexts[-1].scanned_revision == task.artefact["tree_sha256"]
    # voided approvals stay unchanged in the record except status
    rows = {r["approval_id"]: r for r in env.approvals.rows(task.task_id)}
    for o in old:
        assert rows[o["approval_id"]]["bindings_json"] == o["bindings_json"] and rows[o["approval_id"]]["status"] == "voided"
    with pytest.raises(ApprovalError):
        env.approvals.grant_b(task.task_id, role="em", eligibility=env.eligibility, now=env.now)
    env.approvals.grant_a(task.task_id, role="em", eligibility=env.eligibility, now=env.now)
    env.preparer.prepare(task.task_id, env.now)
    env.approvals.grant_b(task.task_id, role="em", eligibility=env.eligibility, now=env.now)
    assert env.tasks.get(task.task_id).state == TaskState.APPROVED_B


def test_refresh_refused_after_publication_and_blocked_voids():
    env = make_env()
    task = env.to_published()
    with pytest.raises(RefreshError):
        security_refresh(task.task_id, role="em", tasks=env.tasks, approvals=env.approvals, scanner=env.scanner,
                         gate=env.gate, now=env.now)
    env2 = make_env()
    t2 = env2.to_prepared()
    env2.gate.next_state = SecurityState.BLOCKED
    out = security_refresh(t2.task_id, role="em", tasks=env2.tasks, approvals=env2.approvals, scanner=env2.scanner,
                           gate=env2.gate, now=env2.now)
    assert out["state"] == "voided"


def test_idempotency_key_replays_and_conflicts():
    from atlas.execution.db import IdempotencyConflict

    env = make_env()
    task = env.new_task()
    a1 = env.approvals.grant_a(task.task_id, role="em", eligibility=env.eligibility, now=env.now, idempotency_key="x")
    a2 = env.approvals.grant_a(task.task_id, role="em", eligibility=env.eligibility, now=env.now, idempotency_key="x")
    assert a1 == a2
    other = env.new_task()
    with pytest.raises(IdempotencyConflict):
        env.approvals.grant_a(other.task_id, role="em", eligibility=env.eligibility, now=env.now, idempotency_key="x")
