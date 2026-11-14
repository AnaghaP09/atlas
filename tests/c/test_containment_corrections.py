import threading

import pytest

from atlas.containment import CONSUMERS, PUBLICATION, RULES, ContainmentError, ContainmentStore
from atlas.execution.corrections import CorrectionError, sha256
from atlas.execution.publication import PublicationRefused
from tests.c.fakes import LostResponse, REPO, make_env


@pytest.mark.parametrize("row", sorted(RULES))
def test_each_row_blocks_its_consumers_and_persists(row, tmp_path):
    from atlas.execution.db import OpsDB

    db = OpsDB(tmp_path / "ops.sqlite")
    store = ContainmentStore(db)
    kw = {"publication_id": "pr:x#1", "offending_item_id": "item-1"} if row in (8, 10) else {}
    from datetime import datetime, timezone

    store.trip(row, producer="test", detail={}, now=datetime.now(timezone.utc), **kw)
    db.close()
    store2 = ContainmentStore(OpsDB(tmp_path / "ops.sqlite"))
    for consumer in CONSUMERS:
        assert store2.permits(consumer).allowed is (consumer not in RULES[row].blocks)


def test_resumption_refused_without_prerequisites_and_needs_audit():
    env = make_env()
    inc = env.containment.trip(4, producer="gateway", detail={}, now=env.now)
    with pytest.raises(ContainmentError, match="missing prerequisite"):
        env.containment.resume(inc, [], role="audit", now=env.now)
    a = env.containment.record_prerequisite(inc, "devops_audit", produced_by="api", detail={}, now=env.now)
    s = env.containment.record_prerequisite(inc, "owner_signoff", produced_by="api", detail={}, now=env.now)
    with pytest.raises(ContainmentError):
        env.containment.resume(inc, [a, s], role="em", now=env.now)
    env.containment.resume(inc, [a, s], role="audit", now=env.now)
    assert env.containment.permits("model_calls").allowed
    assert env.db.events("containment.resumed")


def test_prerequisites_from_another_incident_or_wrong_run_rejected():
    env = make_env()
    first = env.containment.trip(7, producer="execution", detail={}, now=env.now)
    old = [env.containment.record_prerequisite(first, k, produced_by="api", detail={}, now=env.now)
           for k in ("investigation", "golden_case", "owner_signoff")]
    run = env.containment.record_prerequisite(first, "regression_run", produced_by="harness",
                                              detail={"suite": "golden-regression", "revision": "rev-1", "passed": True}, now=env.now)
    env.containment.resume(first, old + [run], role="audit", now=env.now)
    second = env.containment.trip(7, producer="execution", detail={}, now=env.now)
    with pytest.raises(ContainmentError, match="another incident"):
        env.containment.resume(second, old + [run], role="audit", now=env.now)
    fresh = [env.containment.record_prerequisite(second, k, produced_by="api", detail={}, now=env.now)
             for k in ("investigation", "golden_case", "owner_signoff")]
    wrong_suite = env.containment.record_prerequisite(second, "regression_run", produced_by="harness",
                                                      detail={"suite": "unit", "revision": "rev-1", "passed": True}, now=env.now)
    with pytest.raises(ContainmentError, match="wrong suite"):
        env.containment.resume(second, fresh + [wrong_suite], role="audit", now=env.now)
    stale = env.containment.record_prerequisite(second, "regression_run", produced_by="harness",
                                                detail={"suite": "golden-regression", "revision": "rev-0", "passed": True}, now=env.now)
    with pytest.raises(ContainmentError, match="deployed revision"):
        env.containment.resume(second, fresh + [stale], role="audit", now=env.now)
    with pytest.raises(ContainmentError):
        env.containment.resume(second, fresh + ["pre-fabricated"], role="audit", now=env.now)


def test_generic_endpoint_cannot_create_restricted_kinds():
    env = make_env()
    inc = env.containment.trip(8, producer="adjudication", detail={}, now=env.now, publication_id="pr:x#1",
                               offending_item_id="i")
    for kind in ("correction", "regression_run", "eval_run", "sandbox_escape_test_run"):
        with pytest.raises(ContainmentError):
            env.containment.record_prerequisite(inc, kind, produced_by="api", detail={"verified": True}, now=env.now)


def test_injection_row_voids_pending_approvals_and_blocks_other_tasks():
    env = make_env()
    t1 = env.to_approved_b()
    t2 = env.new_task()
    env.containment.trip(7, producer="execution", detail={"run_id": "run-1"}, now=env.now)
    assert all(r["status"] == "voided" for r in env.approvals.rows(t1.task_id))
    from atlas.approvals import ApprovalError

    with pytest.raises(ApprovalError, match="containment"):
        env.approvals.grant_a(t2.task_id, role="em", eligibility=env.eligibility, now=env.now)


# -- corrections -------------------------------------------------------------------------------


def _published_incident(env):
    task = env.to_published()
    pub = f"pr:{REPO}#{task.pr_number}"
    inc = env.containment.trip(8, producer="adjudication", detail={}, now=env.now, publication_id=pub,
                               offending_item_id="claim-3")
    return task, pub, inc


def _authorise(env, inc, text="Corrected: the claim about the cause was withdrawn.", expected=None):
    read = env.corrections.preauthorisation_read(inc, role="audit")
    return env.corrections.authorise(inc, replacement_text=text, expected_body_sha256=expected or read["body_sha256"],
                                     role="audit", now=env.now)


def test_full_trip_correct_resume_sequence_after_approval_b_expired():
    env = make_env()
    task, pub, inc = _published_incident(env)
    env.clock.advance(hours=60)  # original Approval B expired
    other = env.to_approved_b()
    with pytest.raises(PublicationRefused, match="containment"):
        env.publisher.publish(other.task_id, env.now)
    auth = _authorise(env, inc)
    result = env.corrections.execute(auth, now=env.now)
    assert result["state"] == "done" and env.api.body_writes == 2  # one at publication, one correction
    sign = env.containment.record_prerequisite(inc, "owner_signoff", produced_by="api", detail={}, now=env.now)
    cause = env.containment.record_prerequisite(inc, "cause_recorded", produced_by="api", detail={}, now=env.now)
    env.containment.resume(inc, [result["verified_record_id"], cause, sign], role="audit", now=env.now)
    env.publisher.publish(other.task_id, env.now)
    assert env.db.events("report.corrected")[0]["correction_version"] == 1
    assert env.corrections.list(inc)[0]["state"] == "done"
    assert [c["state"] for c in env.containment.incident(inc)["corrections"]] == ["done"]


def test_correction_for_another_item_or_incident_rejected():
    env = make_env()
    task, pub, inc = _published_incident(env)
    other_inc = env.containment.trip(8, producer="adjudication", detail={}, now=env.now, publication_id=pub,
                                     offending_item_id="claim-9")
    result = env.corrections.execute(_authorise(env, other_inc), now=env.now)
    sign = env.containment.record_prerequisite(inc, "owner_signoff", produced_by="api", detail={}, now=env.now)
    cause = env.containment.record_prerequisite(inc, "cause_recorded", produced_by="api", detail={}, now=env.now)
    with pytest.raises(ContainmentError):
        env.containment.resume(inc, [result["verified_record_id"], cause, sign], role="audit", now=env.now)


def test_stale_body_refused_and_combined_authorisation_succeeds():
    env = make_env()
    task, pub, inc = _published_incident(env)
    inc2 = env.containment.trip(8, producer="adjudication", detail={}, now=env.now, publication_id=pub,
                                offending_item_id="claim-4")
    a1 = _authorise(env, inc, text="Body with item 3 corrected.")
    a2 = _authorise(env, inc2, text="Body with item 4 corrected.")
    assert env.corrections.execute(a1, now=env.now)["state"] == "done"
    with pytest.raises(CorrectionError, match="stale"):
        env.corrections.execute(a2, now=env.now)
    combined = _authorise(env, inc2, text="Body with items 3 and 4 corrected.")
    assert env.corrections.execute(combined, now=env.now)["state"] == "done"
    assert env.api.pulls[REPO][0].body == "Body with items 3 and 4 corrected."


def test_third_party_edit_is_stale():
    env = make_env()
    task, pub, inc = _published_incident(env)
    auth = _authorise(env, inc)
    env.api.pulls[REPO][0].body = "edited by someone else"
    with pytest.raises(CorrectionError, match="stale"):
        env.corrections.execute(auth, now=env.now)


def test_lost_response_is_held_then_reconciled_without_second_write():
    env = make_env()
    task, pub, inc = _published_incident(env)
    auth = _authorise(env, inc)
    writes = env.api.body_writes
    env.api.lose_after.add("replace_body")
    held = env.corrections.execute(auth, now=env.now)
    assert held["state"] == "held"
    assert "correction.held" in [n["event_type"] for n in env.notifier.records]
    again = env.corrections.execute(auth, now=env.now)  # repeat reconciles, never re-sends
    assert again["state"] == "done" and env.api.body_writes == writes + 1


def test_slow_first_write_read_before_completion_stays_held():
    env = make_env()
    task, pub, inc = _published_incident(env)
    auth = _authorise(env, inc)
    env.api.fail_before.add("replace_body")  # outcome uncertain; the body was not changed
    held = env.corrections.execute(auth, now=env.now)
    assert held["state"] == "held"
    writes = env.api.body_writes
    again = env.corrections.execute(auth, now=env.now)
    assert again["state"] == "held" and env.api.body_writes == writes  # never re-sent automatically


def test_crash_after_remote_write_before_local_commit(tmp_path):
    from tests.c.fakes import make_env as mk

    env = mk(db_path=tmp_path / "ops.sqlite")
    task, pub, inc = _published_incident(env)
    auth = _authorise(env, inc)
    # simulate: intent row committed, remote write done, process died before recording success
    from atlas.execution.db import iso, new_id

    cid = new_id("corr")
    with env.db.transaction() as conn:
        conn.execute("INSERT INTO corrections (correction_id, authorisation_id, publication_id, state, at) VALUES (?,?,?,?,?)",
                     (cid, auth, pub, "intent", iso(env.now)))
    env.api.replace_pull_body(REPO, task.pr_number, "Corrected: the claim about the cause was withdrawn.")
    writes = env.api.body_writes
    result = env.corrections.execute(auth, now=env.now)
    assert result["state"] == "done" and env.api.body_writes == writes


def test_concurrent_requests_same_authorisation_one_write():
    env = make_env()
    task, pub, inc = _published_incident(env)
    auth = _authorise(env, inc)
    writes = env.api.body_writes
    results = []
    threads = [threading.Thread(target=lambda: results.append(env.corrections.execute(auth, now=env.now))) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert env.api.body_writes == writes + 1 and all(r["state"] == "done" for r in results)


def test_expired_authorisation_and_non_audit_refused():
    env = make_env()
    task, pub, inc = _published_incident(env)
    auth = _authorise(env, inc)
    env.clock.advance(hours=25)
    with pytest.raises(CorrectionError, match="expired"):
        env.corrections.execute(auth, now=env.now)
    with pytest.raises(CorrectionError):
        env.corrections.preauthorisation_read(inc, role="em")


def test_correction_api_offers_no_comment_push_or_new_pr():
    import inspect

    from atlas.execution import corrections

    source = inspect.getsource(corrections)
    for forbidden in ("push_branch", "create_draft_pull", "comment"):
        assert forbidden not in source.replace("No comment", "").replace("no comment", "")
