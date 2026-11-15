from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from atlas.contracts import Mode, Principal, Role, SecurityContext, SecurityState
from atlas.hitl import HumanQueue, triggers_for_run
from atlas.notify import Destination, NotificationService
from atlas.projection import PUBLIC, AuthorizationProjection, DisclosureBlocked, channel_principal_for, narrow, principal_for
from atlas.security_gate import (
    Finding,
    ScanRun,
    SecurityException,
    SecurityGate,
    evaluate,
    parse_security_records,
    revalidate,
)
from tests.b.helpers import EventSink, make_record
from tests.fakes import FakeClock

NOW = datetime(2026, 7, 30, 12, tzinfo=timezone.utc)
COMPS = {"network", "storage"}


def scan(sid="s1", hours_old=1, coverage=COMPS, status="success", findings=()):
    return ScanRun(sid, NOW - timedelta(hours=hours_old), frozenset(coverage), status, tuple(findings))


# --- security gate ------------------------------------------------------------------


def test_zero_finding_scan_covering_everything_passes() -> None:
    r = evaluate(COMPS, [scan()], [], [], "p1", NOW)
    assert r.state == SecurityState.PASSED and r.valid_until == NOW - timedelta(hours=1) + timedelta(hours=24)


@pytest.mark.parametrize(
    "scans",
    [
        [],  # skipped scan
        [scan(status="error")],  # connector failure
        [scan(status="partial_failure")],
        [scan(hours_old=25)],  # stale
        [scan(coverage={"network"})],  # component omitted
    ],
)
def test_not_run_cases(scans) -> None:
    r = evaluate(COMPS, scans, [], [], "p1", NOW)
    assert r.state == SecurityState.NOT_RUN and r.valid_until is None


def test_blocking_rules_and_exceptions() -> None:
    f_high_fix = Finding("f1", "network", "high", "1.2.3")
    f_high_nofix = Finding("f2", "network", "high", None)
    f_missing = Finding("f3", "storage", None, None)
    assert evaluate(COMPS, [scan(findings=["f2"])], [f_high_nofix], [], "p1", NOW).state == SecurityState.PASSED
    assert evaluate(COMPS, [scan(findings=["f1"])], [f_high_fix], [], "p1", NOW).state == SecurityState.BLOCKED
    assert evaluate(COMPS, [scan(findings=["f3"])], [f_missing], [], "p1", NOW).state == SecurityState.BLOCKED
    good = SecurityException("e1", "f1", "sec-lead", "fix next sprint", NOW + timedelta(hours=5))
    r = evaluate(COMPS, [scan(findings=["f1"])], [f_high_fix], [good], "p1", NOW)
    assert r.state == SecurityState.ACCEPTED_WITH_EXCEPTION and r.signoff_required and r.valid_until == NOW + timedelta(hours=5)
    for bad in (
        SecurityException("e2", "f1", None, "r", NOW + timedelta(hours=5)),
        SecurityException("e3", "f1", "a", "r", NOW - timedelta(seconds=1)),
        SecurityException("e4", "fX", "a", "r", NOW + timedelta(hours=5)),
        SecurityException("e5", "f1", "a", "r", NOW + timedelta(hours=5), revoked=True),
    ):
        assert evaluate(COMPS, [scan(findings=["f1"])], [f_high_fix], [bad], "p1", NOW).state == SecurityState.BLOCKED


def test_revalidate_expiry_policy_and_exception_changes() -> None:
    r = evaluate(COMPS, [scan(hours_old=1)], [], [], "p1", NOW)
    assert revalidate(r, NOW, "p1") == (True, "valid")
    assert not revalidate(r, NOW + timedelta(hours=23, seconds=1), "p1")[0]  # scan passes 24 hours
    assert not revalidate(r, NOW, "p2")[0]  # policy changed
    exc = SecurityException("e1", "f1", "a", "r", NOW + timedelta(hours=2))
    r2 = evaluate(COMPS, [scan(findings=["f1"])], [Finding("f1", "network", "critical", "2")], [exc], "p1", NOW)
    lookup = {"e1": exc}.get
    assert not revalidate(r2, NOW, "p1", lookup)[0]  # sign-off missing
    assert revalidate(r2, NOW, "p1", lookup, signed_off=True)[0]
    assert not revalidate(r2, NOW + timedelta(hours=3), "p1", lookup, signed_off=True)[0]  # exception expired
    revoked = SecurityException("e1", "f1", "a", "r", NOW + timedelta(hours=2), revoked=True)
    assert not revalidate(r2, NOW, "p1", {"e1": revoked}.get, signed_off=True)[0]
    assert not revalidate(evaluate(COMPS, [], [], [], "p1", NOW), NOW, "p1")[0]


def sec_record(rid, data, confirmed):
    return make_record(rid, "security", json.dumps(data), confirmed_through=confirmed)


def test_investigation_and_task_read_paths(run_context) -> None:
    from atlas.agents.resolver import EvidenceResolver
    from tests.fakes import InMemoryStore

    store = InMemoryStore()
    store.snapshot_for_write = run_context.snapshot_id
    store.put(sec_record("sr1", {"kind": "scan_run", "scan_run_id": "sr1", "coverage": sorted(COMPS), "status": "success",
                                 "finding_ids": []}, run_context.evidence_clock - timedelta(hours=2)), "security-adapter")
    gate = SecurityGate("p1", lambda: NOW)
    assert gate.evaluate_for_run(EvidenceResolver(store, run_context), COMPS).state == SecurityState.PASSED

    class Reader:
        def __init__(self):
            self.partitions = {("dest", "act-1", "rev1"): [sec_record("t1", {"kind": "scan_run", "scan_run_id": "t1", "coverage": sorted(COMPS),
                                                                              "status": "success", "finding_ids": [], "target_revision": "rev1"}, NOW)]}
            self.asked = []

        def read(self, ctx):
            self.asked.append(ctx)
            return self.partitions.get((ctx.destination_repo, ctx.action_id, ctx.scanned_revision), [])

    reader = Reader()
    ok = gate.evaluate_for_task(reader, SecurityContext("dest", "act-1", "rev1"), COMPS)
    other = gate.evaluate_for_task(reader, SecurityContext("dest", "act-2", "rev1"), COMPS)
    assert ok.state == SecurityState.PASSED and other.state == SecurityState.NOT_RUN
    with pytest.raises(TypeError):
        gate.evaluate_for_task(reader, run_context, COMPS)
    assert ok.decision_id != gate.evaluate_for_task(reader, SecurityContext("dest", "act-1", "rev1"), COMPS).decision_id


def test_unreadable_security_record_is_not_clean() -> None:
    scans, findings = parse_security_records([make_record("x", "security", "not json")])
    assert scans == [] and findings == []


# --- projection -------------------------------------------------------------------------


def restricted_artifact():
    secret_item = {"text": "CVE detail for vulnerable module", "required_scopes": ["restricted:security"], "claim_ids": []}
    return {
        "required_scopes": [],
        "findings": [secret_item, {"text": "public finding", "required_scopes": ["public"], "claim_ids": []}],
        "reasoning": [secret_item],
        "graph": {"nodes": [{"id": "n1", "required_scopes": ["restricted:security"]}, {"id": "n2", "required_scopes": ["public"]}]},
        "queue": [{"reason": "blocked", "required_scopes": ["restricted:security"]}],
        "approval": {"diff_summary": "x", "required_scopes": ["restricted:security"]},
        "event": {"type": "notification.created", "required_scopes": ["restricted:security"]},
        "scorecard": [{"row": 1, "required_scopes": ["restricted:security"]}],
        "chat": {"text": "y", "required_scopes": ["restricted:security"]},
        "publication": {"pr_title": {"text": "t", "required_scopes": ["restricted:security"]},
                        "pr_body": {"text": "b", "required_scopes": ["restricted:security"]},
                        "commit_message": {"text": "c", "required_scopes": ["restricted:security"]},
                        "files": [{"path": "a.go", "required_scopes": ["restricted:security"]}]},
        "unknown": {"text": "z", "required_scopes": None},
    }


@pytest.mark.parametrize("principal", [principal_for("oncall"), channel_principal_for("pm"), PUBLIC])
def test_restricted_record_never_reaches_unauthorised_principal(principal) -> None:
    out = AuthorizationProjection().project(restricted_artifact(), principal)
    blob = json.dumps(out)
    for marker in ("CVE detail", '"n1"', "blocked", "diff_summary", "pr_title\": {", "a.go", '"z"'):
        assert marker not in blob, marker
    assert out["restricted_items"] >= 10 and "public finding" in blob
    sec = AuthorizationProjection().project(restricted_artifact(), principal_for("security"))
    assert "CVE detail" in json.dumps(sec) and '"z"' not in json.dumps(sec)  # unknown scope withheld even here


@pytest.mark.parametrize("role", ["pm", "em", "architect"])
def test_main_roles_read_restricted_content_but_never_unknown_scopes(role) -> None:
    out = json.dumps(AuthorizationProjection().project(restricted_artifact(), principal_for(role)))
    assert "CVE detail" in out and '"z"' not in out


def test_unknown_scope_visible_only_on_audit_endpoint() -> None:
    p = AuthorizationProjection()
    assert '"z"' not in json.dumps(p.project(restricted_artifact(), principal_for("audit")))
    assert '"z"' in json.dumps(p.project_for_audit_endpoint(restricted_artifact(), principal_for("audit")))
    with pytest.raises(PermissionError):
        p.project_for_audit_endpoint(restricted_artifact(), principal_for("em"))


def test_caller_supplied_reader_cannot_widen() -> None:
    oncall = principal_for("oncall")
    widened = narrow(oncall, "security")
    assert "restricted:security" not in widened.scopes
    assert "CVE detail" not in json.dumps(AuthorizationProjection().project(restricted_artifact(), widened))


def test_public_projection_blocks_personal_data() -> None:
    sink = EventSink()
    p = AuthorizationProjection(events=sink)
    with pytest.raises(DisclosureBlocked):
        p.project({"required_scopes": [], "pr_body": {"text": "thanks ops@example.org", "required_scopes": ["public"]}}, PUBLIC)
    assert sink.types() == ["unauthorized_disclosure_blocked"]
    assert p.project({"required_scopes": [], "pr_body": {"text": "fix ipv6 test", "required_scopes": ["public"]}}, PUBLIC)


# --- queue ---------------------------------------------------------------------------------


def test_queue_dedup_and_projection() -> None:
    q = HumanQueue()
    a, created = q.add("security_blocked", "run-1", "run-1", "blocked", [{"text": "cve", "required_scopes": ["restricted:security"]}])
    b, again = q.add("security_blocked", "run-1", "run-1", "blocked")
    assert created and not again and a == b
    q.add("scorecard_class_negative", "class:deps", None, "class turned negative")  # after the run finished
    view = q.view(AuthorizationProjection(), principal_for("oncall"))
    assert len(view) == 2 and "cve" not in json.dumps(view)


def test_box12_triggers_from_run_output() -> None:
    out = {"confidence": 0.5, "security_check": {"state": "not_run"}, "verifier": {"claims_rejected": 3},
           "injection_detected": True, "run_status": "unverified",
           "insufficient_evidence": [{"text": "owner unknown", "rank": 1}],
           "forecast": {"requirements_at_risk": ["1"], "slip_days": 9}}
    names = {t for t, _ in triggers_for_run(out, budget_exhausted="time", payload_withheld=True)}
    assert names == {"low_confidence", "security_not_run", "verifier_rejections", "injection_detected", "run_unverified",
                     "budget_exceeded", "payload_withheld", "insufficient_evidence_high_rank", "requirements_at_risk", "forecast_slip"}


# --- notifications -----------------------------------------------------------------------


class Webhook:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def call(self, role, purpose, payload):
        self.calls.append(payload)
        if self.fail:
            raise ConnectionError("down")


def svc(**kw):
    clock = kw.pop("clock", FakeClock())
    return NotificationService(projection=AuthorizationProjection(), principal_for=channel_principal_for, clock=clock.now, **kw), clock


def test_catalog_drives_severity_roles_and_caller_cannot_override() -> None:
    s, _ = svc()
    nid = s.emit("spend.80", "ceiling", [], frozenset({"internal"}))
    rec = s.records()[0]
    assert rec["severity"] == "warning" and set(rec["roles"]) == {"em", "audit"}
    s.emit("approval.nearing_expiry", "apr-1", [], frozenset({"internal"}))
    assert s.records()[1]["kind"] == "reminder"
    with pytest.raises(KeyError):
        s.emit("made.up", "x", [], frozenset())
    with pytest.raises(TypeError):
        s.emit("spend.80", "x", [], frozenset(), severity="info")  # type: ignore[call-arg]


def test_dedup_window_and_critical_bypasses_rate_limit() -> None:
    s, clock = svc(rate_limit_per_role=1)
    a = s.emit("run.failed", "run-1", [], frozenset({"internal"}))
    assert s.emit("run.failed", "run-1", [], frozenset({"internal"})) == a
    clock.advance(minutes=11)
    assert s.emit("run.failed", "run-1", [], frozenset({"internal"})) != a
    s.emit("containment.tripped", "inc-1", [], frozenset({"internal"}))
    em_view = s.in_app(principal_for("em"))
    assert any(n["severity"] == "critical" for n in em_view)


def test_restricted_content_withheld_for_role_without_scope() -> None:
    s, _ = svc()
    s.emit("security_gate.blocked", "run-2", [], frozenset({"restricted:security"}),
           {"text": "CVE in module x", "required_scopes": ["restricted:security"]})
    narrow_em = Principal(role=Role.EM, scopes=frozenset({"public", "internal", "staffing"}))
    em = s.in_app(narrow_em)
    sec = s.in_app(principal_for("security"))
    assert "CVE" not in json.dumps(em) and em[0]["restricted_items"] >= 1
    assert "CVE" in json.dumps(sec)
    assert "CVE" in json.dumps(s.in_app(principal_for("em")))  # the engineering manager reads everything


def test_failing_webhook_retries_then_dead_letter_and_in_app_notice_only() -> None:
    hook = Webhook(fail=True)
    s, _ = svc(outbound=hook, destinations=[Destination("chat", "http://127.0.0.1:9/h", "em")], max_attempts=3)
    s.emit("run.failed", "run-3", [], frozenset({"internal"}))
    assert len(hook.calls) == 3
    assert s.deliveries("dead_letter") and any(r["event_type"] == "delivery.dead_lettered" for r in s.records())
    assert len(hook.calls) == 3  # the failure notice never went back through the webhook


def test_offline_and_containment_pause_external_delivery_then_resume_once() -> None:
    mode = {"m": Mode.OFFLINE}
    hook = Webhook()
    s, _ = svc(outbound=hook, destinations=[Destination("chat", "http://127.0.0.1:9/h", "em")], mode=lambda: mode["m"])
    s.emit("run.failed", "run-4", [], frozenset({"internal"}))
    assert hook.calls == [] and s.deliveries("paused") and s.in_app(principal_for("em"))
    mode["m"] = Mode.CONNECTED
    assert s.resume_paused() == 1 and len(hook.calls) == 1 and s.resume_paused() == 0
    allowed = {"v": False}
    hook2 = Webhook()
    s2, _ = svc(outbound=hook2, destinations=[Destination("chat", "http://127.0.0.1:9/h", "em")], external_allowed=lambda: allowed["v"])
    s2.emit("run.failed", "run-5", [], frozenset({"internal"}))
    assert hook2.calls == [] and s2.deliveries("paused")[0]["reason"] == "containment"


def test_shared_destination_audience_projection_and_undeclared_audience() -> None:
    hook = Webhook()
    s, _ = svc(outbound=hook, destinations=[Destination("shared", "http://127.0.0.1:9/h", "pm"),
                                             Destination("unset", "http://127.0.0.1:9/u", None)])
    s.emit("security_gate.blocked", "run-6", [], frozenset({"restricted:security"}),
           {"text": "CVE in module y", "required_scopes": ["restricted:security"]})
    # a chat channel for the product manager keeps the narrow scopes: restricted content never leaves
    assert len(hook.calls) == 1 and "CVE" not in json.dumps(hook.calls[0])
    held = [d for d in s.deliveries() if d["destination"] == "unset"]
    assert held[0]["status"] == "held" and held[0]["reason"] == "audience_undeclared"
    notices = [r for r in s.records() if r["event_type"] == "delivery.audience_undeclared"]
    assert len(notices) == 1 and "unset" in json.dumps(notices[0]["content"])
    assert all(c["url"].endswith("/h") for c in hook.calls)


def test_notifier_fault_in_audit_log_and_health() -> None:
    class Broken:
        def call(self, *a):
            raise RuntimeError("boom")

    s, _ = svc(outbound=Broken(), destinations=[Destination("chat", "http://127.0.0.1:9/h", "em")], max_attempts=1)
    s.principal_for = lambda r: (_ for _ in ()).throw(ValueError("bad audience"))
    s.emit("run.failed", "run-7", [], frozenset({"internal"}))
    assert s.records() and s.audit_log() and s.health()["notifier"] == "degraded"
