import threading

import pytest

from atlas.contracts import TaskState
from atlas.execution import artefact as artefacts
from atlas.execution.artefact import ArtefactError
from atlas.execution.publication import PublicationRefused, PublicationStopped
from tests.c.fakes import FIX_DIFF, GitBackedPublicationAPI, LostResponse, REPO, make_env

THREE_FILE_DIFF = FIX_DIFF + """--- /dev/null
+++ b/netaddr/notes.go
@@ -0,0 +1,2 @@
+package netaddr
+// internal note
--- /dev/null
+++ b/netaddr/extra.go
@@ -0,0 +1,2 @@
+package netaddr
+// extra helper
"""


def _env(**kw):
    env = make_env(**kw)
    return env


def test_publish_end_to_end_one_branch_one_pr():
    env = _env()
    task = env.to_published()
    assert task.state == TaskState.PUBLISHED and task.support_status == "supported"
    assert task.pr_number == 1 and task.published_commit
    pr = env.api.pulls[REPO][0]
    assert pr.draft and pr.body == task.artefact["body"] and pr.head == task.publication_ref
    assert [r["step"] for r in env.publisher.journal(task.action_id) if r["status"] == "done"] == ["push", "pull_request", "description"]
    # repeating publish does nothing remote
    calls = len(env.api.calls)
    env.publisher.publish(task.task_id, env.now)
    assert "push" not in env.api.calls[calls:] and "create_pull" not in env.api.calls[calls:]


def test_publish_with_local_bare_git(tmp_path):
    api = GitBackedPublicationAPI(tmp_path)
    env = _env(api=api)
    task = env.new_task(base_commit=api.base_commit)
    task = env.to_published(task)
    head = api.branch_head(REPO, task.publication_ref)
    assert head == task.published_commit
    import subprocess

    shown = subprocess.run(["git", "--git-dir", str(api.bare), "show", f"{head}:netaddr/netaddr.go"],
                           capture_output=True, text=True, check=True).stdout
    assert 'strings.Contains(host, ":")' in shown


@pytest.mark.parametrize("step", ["push", "create_pull", "replace_body"])
def test_lost_response_after_each_remote_effect_is_reconciled(step):
    env = _env()
    task = env.to_approved_b()
    env.api.lose_after.add(step)
    with pytest.raises(LostResponse):
        env.publisher.publish(task.task_id, env.now)
    env.publisher.publish(task.task_id, env.now)
    assert len([b for b in env.api.branches if b[1] == task.publication_ref]) == 1
    assert len(env.api.pulls[REPO]) == 1
    assert env.tasks.get(task.task_id).state == TaskState.PUBLISHED


@pytest.mark.parametrize("step", ["push", "create_pull", "replace_body"])
def test_failure_before_remote_effect_retries_cleanly(step):
    env = _env()
    task = env.to_approved_b()
    env.api.fail_before.add(step)
    with pytest.raises(ConnectionError):
        env.publisher.publish(task.task_id, env.now)
    env.publisher.publish(task.task_id, env.now)
    assert len(env.api.pulls[REPO]) == 1


def test_crash_after_intent_row_before_remote_write_across_restart(tmp_path):
    env = _env(db_path=tmp_path / "ops.sqlite")
    task = env.to_approved_b()
    env.publisher._write(task.action_id, "push", "intent", env.now)  # crash point: intent committed, nothing sent
    env.db.close()
    from tests.c.fakes import make_env as mk

    env2 = mk(db_path=tmp_path / "ops.sqlite", api=env.api)
    env2.gate.decisions.update(env.gate.decisions)
    env2.publisher.publish(task.task_id, env2.now)
    assert len(env.api.pulls[REPO]) == 1 and env2.tasks.get(task.task_id).state == TaskState.PUBLISHED


def test_closed_pull_request_found_on_recovery_stops():
    env = _env()
    task = env.to_approved_b()
    env.api.lose_after.add("create_pull")
    with pytest.raises(LostResponse):
        env.publisher.publish(task.task_id, env.now)
    env.api.pulls[REPO][0].state = "closed"
    with pytest.raises(PublicationStopped):
        env.publisher.publish(task.task_id, env.now)
    with pytest.raises(PublicationStopped):
        env.publisher.publish(task.task_id, env.now)
    assert len(env.api.pulls[REPO]) == 1


def test_approval_expiring_during_recovery_refuses_further_writes():
    env = _env()
    task = env.to_approved_b()
    env.api.fail_before.add("create_pull")
    with pytest.raises(ConnectionError):
        env.publisher.publish(task.task_id, env.now)
    env.clock.advance(hours=49)
    with pytest.raises(PublicationRefused, match="expired"):
        env.publisher.publish(task.task_id, env.now)
    assert REPO not in env.api.pulls


def test_concurrent_publish_requests_create_one_pr():
    env = _env()
    task = env.to_approved_b()
    errors = []

    def run():
        try:
            env.publisher.publish(task.task_id, env.now)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert len(env.api.pulls[REPO]) == 1 and env.api.calls.count("push") == 1


def test_non_executing_sandbox_never_reaches_publication():
    from atlas.approvals import ApprovalError
    from atlas.execution.runner import NoExecutionRunner
    from atlas.execution.sandbox import NonExecutingSandbox

    env = _env(sandbox=NoExecutionRunner(NonExecutingSandbox(gofmt=None)))
    task = env.to_prepared()
    assert task.checks["go_vet"] == "unavailable" and task.checks["container_sandbox"] == "unavailable"
    with pytest.raises(ApprovalError, match="container_sandbox"):
        env.approvals.grant_b(task.task_id, role="em", eligibility=env.eligibility, now=env.now)


def test_upstream_destination_refused():
    env = _env()
    task = env.to_approved_b()
    env.db.query("UPDATE tasks SET destination_repo = 'kubevirt/kubevirt' WHERE task_id = ?", (task.task_id,))
    with pytest.raises(PublicationRefused, match="upstream"):
        env.publisher.publish(task.task_id, env.now)


def test_every_field_projected_for_public_and_fails_closed_when_empty():
    env = _env()
    task = env.new_task()
    env.approvals.grant_a(task.task_id, role="em", eligibility=env.eligibility, now=env.now)
    with pytest.raises(ArtefactError, match="body"):
        env.preparer.prepare(task.task_id, env.now, field_scopes={"body": ["restricted"]})


def test_partial_withholding_and_projection_change_abort():
    env = _env(diff=THREE_FILE_DIFF)
    task = env.new_task()
    env.db.query("UPDATE tasks SET scope_json = ? WHERE task_id = ?", ('["netaddr/*.go"]', task.task_id))
    env.approvals.grant_a(task.task_id, role="em", eligibility=env.eligibility, now=env.now)
    # whole file notes.go restricted; one hunk of netaddr.go restricted (it has only one, so use extra.go as the hunk case)
    env.preparer.prepare(task.task_id, env.now, source_scopes={"netaddr/notes.go": ["restricted"],
                                                                 "netaddr/extra.go#0": ["restricted"]})
    t = env.tasks.get(task.task_id)
    paths = {w["path"]: w["withheld"] for w in t.artefact["withheld"]}
    assert paths == {"netaddr/notes.go": "whole file", "netaddr/extra.go": "1 hunk(s)"}
    assert "notes.go" not in t.artefact["diff"] and "extra.go" not in t.artefact["diff"]
    assert set(env.sandbox.trees[-1]) >= {"netaddr/netaddr.go"} and "netaddr/notes.go" not in env.sandbox.trees[-1]
    env.approvals.grant_b(task.task_id, role="em", eligibility=env.eligibility, now=env.now)
    # the projection now withholds more: outgoing artefact no longer matches -> abort, void, containment row 2
    frozen = dict(t.artefact)
    frozen["source"][0]["hunks"][0]["required_scopes"] = ["restricted"]
    import json

    env.tasks.update_fields(task.task_id, env.now, artefact_json=json.dumps(frozen))
    with pytest.raises(PublicationRefused):
        env.publisher.publish(task.task_id, env.now)
    assert env.containment.open_incidents()[0]["row"] == 2
    assert env.tasks.get(task.task_id).state == TaskState.APPROVED_A
    assert REPO not in env.api.pulls


def test_reverification_racing_publication_both_outcomes():
    from atlas.execution.lifecycle import apply_reverification

    # re-verification commits first: unpublished disposition, publication refused before any remote effect
    env = _env()
    task = env.to_approved_b()
    env.run_state.generation["run-1"] = 2
    apply_reverification(run_id="run-1", new_generation=2, supported_actions=frozenset({"DELEGATE_TO_CODING_AGENT"}),
                         verification_ok=True, tasks=env.tasks, approvals=env.approvals,
                         journal_complete=env.publisher.complete, now=env.now)
    assert env.tasks.get(task.task_id).state == TaskState.AWAITING_REAPPROVAL
    with pytest.raises(PublicationRefused):
        env.publisher.publish(task.task_id, env.now)
    assert not env.api.calls or "push" not in env.api.calls

    # publication commits first: published state kept, support recorded
    env = _env()
    task = env.to_published()
    env.run_state.generation["run-1"] = 2
    apply_reverification(run_id="run-1", new_generation=2, supported_actions=frozenset(), verification_ok=True,
                         tasks=env.tasks, approvals=env.approvals, journal_complete=env.publisher.complete,
                         now=env.now, notifier=env.notifier)
    t = env.tasks.get(task.task_id)
    assert t.state == TaskState.PUBLISHED and t.support_status == "withdrawn"
    assert "task.support_withdrawn" in [n["event_type"] for n in env.notifier.records]


def test_artefact_build_requires_applicable_diff():
    env = _env()
    base = env.preparer.trees.files(REPO, "x")
    bad = artefacts.source_from_diff(FIX_DIFF.replace('"strconv"', '"fmt"', 1))
    with pytest.raises(ArtefactError):
        artefacts.build(env.projection, base, bad, commit_message="m", title="t", body="b")
