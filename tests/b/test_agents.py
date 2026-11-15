from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from atlas.agents.investigator import Budget, Investigator, build_tools, filter_log
from atlas.agents.resolver import EvidenceResolver
from atlas.agents.verifier import Verifier, VerifierFailed
from atlas.verified import DanglingReference, build_verified, removal_closure
from tests.b.helpers import ScriptedOutbound, make_record
from tests.fakes import FakeClock, InMemoryStore

FIX = Path(__file__).resolve().parents[1] / "contract" / "fixtures"


def claim(cid, verdict="supported", deps=(), ev=("r1",), kind="finding"):
    return {"id": cid, "text": f"claim {cid}", "kind": kind, "evidence_ids": list(ev), "depends_on": list(deps),
            "required_scopes": ["public"], "verdict": verdict, "verdict_reason": None}


def bound(text, ids):
    return {"text": text, "claim_ids": list(ids), "required_scopes": ["public"]}


def artefact(claims):
    return {
        "claims": claims,
        "classification": {"value": "DEPENDENCY", "claim_ids": ["c1"], "required_scopes": ["public"]},
        "action": {"value": "DELEGATE_TO_CODING_AGENT", "claim_ids": ["c1", "c3"], "required_scopes": ["public"]},
        "root_cause": bound("cause", ["c1"]),
        "options": [bound("bump it", ["c3"]), bound("other", ["c4"])],
        "findings": [bound("f", ["c2"])],
        "reasoning": [bound("r", ["c4"])],
        "items": [],
        "evidence_recommendation": "DELEGATE_TO_CODING_AGENT",
    }


def test_rejected_cause_is_removed_from_every_bound_field() -> None:
    a = artefact([claim("c1", "rejected"), claim("c2"), claim("c3"), claim("c4")])
    v = build_verified(a).artefact
    assert v["root_cause"] is None
    assert v["classification"]["value"] is None and v["action"]["value"] is None
    assert v["evidence_recommendation"] is None
    assert [o["text"] for o in v["options"]] == ["bump it", "other"]  # bound to independent claims
    assert a["root_cause"] is not None  # input untouched


def test_classification_keeps_minimum_support_when_one_supporting_claim_is_rejected() -> None:
    a = artefact([claim("c1"), claim("c2", "rejected"), claim("c3"), claim("c4")])
    a["classification"] = {"value": "CODE_DEFECT", "claim_ids": ["c1", "c2"], "required_scopes": ["public"]}
    a["action"] = {"value": "ESCALATE_DECISION", "claim_ids": ["c1", "c2"], "required_scopes": ["public"]}
    v = build_verified(a).artefact
    assert v["classification"] == {"value": "CODE_DEFECT", "claim_ids": ["c1"], "required_scopes": ["public"]}
    assert v["action"]["value"] == "ESCALATE_DECISION" and v["action"]["claim_ids"] == ["c1"]
    assert v["root_cause"] is not None and v["findings"] == []  # the finding bound to c2 is gone


def test_classification_is_removed_when_no_supporting_claim_is_left() -> None:
    a = artefact([claim("c1", "rejected"), claim("c2", "contradicted"), claim("c3"), claim("c4")])
    a["classification"] = {"value": "CODE_DEFECT", "claim_ids": ["c1", "c2"], "required_scopes": ["public"]}
    a["action"] = {"value": "ASSIGN_DEVELOPER", "claim_ids": ["c3"], "required_scopes": ["public"]}
    v = build_verified(a).artefact
    assert v["classification"]["value"] is None
    assert v["action"]["value"] is None  # the action follows the classification


def test_coding_action_needs_every_claim_and_the_root_cause() -> None:
    a = artefact([claim("c1"), claim("c2"), claim("c3", "rejected"), claim("c4")])
    v = build_verified(a).artefact
    assert v["classification"]["value"] == "DEPENDENCY"
    assert v["action"]["value"] == "ASSIGN_DEVELOPER"
    b = artefact([claim("c1"), claim("c2"), claim("c3"), claim("c5", "rejected"), claim("c4")])
    b["root_cause"] = bound("cause", ["c5"])
    w = build_verified(b).artefact
    assert w["root_cause"] is None and w["classification"]["value"] == "DEPENDENCY"
    assert w["action"]["value"] == "ASSIGN_DEVELOPER"


def test_three_hop_chain_pruned_even_when_later_claims_supported() -> None:
    claims = [claim("c1", "rejected"), claim("c2", deps=["c1"]), claim("c3", deps=["c2"]), claim("c4")]
    assert removal_closure(claims) == {"c1", "c2", "c3"}
    v = build_verified(artefact(claims)).artefact
    assert v["findings"] == [] and [o["text"] for o in v["options"]] == ["other"]
    assert {c["id"] for c in v["claims"]} == {"c4"}


@pytest.mark.parametrize("verdict", ["contradicted", "missing"])
def test_contradicted_and_missing_prune_like_rejected(verdict) -> None:
    v = build_verified(artefact([claim("c1"), claim("c2"), claim("c3", verdict), claim("c4")])).artefact
    assert v["action"]["value"] == "ASSIGN_DEVELOPER" and v["classification"]["value"] == "DEPENDENCY"


def test_dangling_reference_fails() -> None:
    with pytest.raises(DanglingReference):
        build_verified(artefact([claim("c1"), claim("c2", deps=["zz"]), claim("c3"), claim("c4")]))
    a = artefact([claim("c1"), claim("c2"), claim("c3"), claim("c4")])
    a["options"].append(bound("x", ["nope"]))
    with pytest.raises(DanglingReference):
        build_verified(a)


def test_contract_fixtures_prune_consistently() -> None:
    for name in ("run_complete", "run_rejected", "run_contradicted", "run_partial"):
        data = json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))
        v = build_verified(data).artefact
        assert all(c["verdict"] == "supported" for c in v["claims"]), name


# --- verifier -------------------------------------------------------------------


def resolver_with(run_context, *records):
    store = InMemoryStore()
    store.snapshot_for_write = run_context.snapshot_id
    for r in records:
        store.put(r, r.writer)
    res = EvidenceResolver(store, run_context)
    for r in records:
        res.get(r.source, r.source_id)
    return res


def fresh(rid, source="git", **kw):
    return make_record(rid, source, f"content of {rid}", **kw)


def test_one_side_of_conflict_is_contradicted_without_model(run_context) -> None:
    res = resolver_with(run_context, fresh("t1", conflicts=("ins-1",)), fresh("g1"))
    out = ScriptedOutbound({"verifier": [{"verdicts": [{"claim_id": "c2", "verdict": "supported", "reason": "ok", "contradicting_record": None}]}]})
    result = Verifier(out, res.by_id).verify([claim("c1", ev=["t1"]), claim("c2", ev=["g1"])])
    v = {c["id"]: c["verdict"] for c in result.claims}
    assert v == {"c1": "contradicted", "c2": "supported"}
    assert result.contradictions and result.contradictions[0]["claim_id"] == "c1"


def test_stale_evidence_rejected_inside_limit_accepted(run_context) -> None:
    clock = run_context.evidence_clock
    stale = make_record("i1", "issues", "x", confirmed_through=clock - timedelta(hours=1, seconds=1))
    ok = make_record("i2", "issues", "y", confirmed_through=clock - timedelta(minutes=59))
    res = resolver_with(run_context, stale, ok)
    out = ScriptedOutbound({"verifier": [{"verdicts": [{"claim_id": "c2", "verdict": "supported", "reason": "ok", "contradicting_record": None}]}]})
    result = Verifier(out, res.by_id).verify([claim("c1", ev=["i1"]), claim("c2", ev=["i2"])])
    v = {c["id"]: c["verdict"] for c in result.claims}
    assert v == {"c1": "rejected", "c2": "supported"}


def test_security_finding_needs_scanner_record_and_missing_verdicts(run_context) -> None:
    res = resolver_with(run_context, fresh("g1"), fresh("g2"))
    out = ScriptedOutbound({"verifier": [{"verdicts": []}]})
    result = Verifier(out, res.by_id).verify([claim("c1", ev=["g1"], kind="security_finding"), claim("c2", ev=["g2"])])
    v = {c["id"]: c["verdict"] for c in result.claims}
    assert v == {"c1": "rejected", "c2": "missing"}


def test_verifier_sees_only_claims_and_cited_records(run_context) -> None:
    res = resolver_with(run_context, fresh("g1"), fresh("g9"))
    out = ScriptedOutbound({"verifier": [{"verdicts": [{"claim_id": "c1", "verdict": "supported", "reason": "ok", "contradicting_record": None}]}]})
    Verifier(out, res.by_id).verify([claim("c1", ev=["g1"])])
    sent = out.calls[0][1]["messages"][0]["content"]
    assert "g1" in sent and "g9" not in sent and "assistant" not in json.dumps(out.calls[0][1]["messages"])


def test_verifier_failure_raises(run_context) -> None:
    res = resolver_with(run_context, fresh("g1"))
    out = ScriptedOutbound({"verifier": [RuntimeError("timeout")]})
    with pytest.raises(VerifierFailed):
        Verifier(out, res.by_id).verify([claim("c1", ev=["g1"])])


# --- investigator ----------------------------------------------------------------


def step_tool(tool, **args):
    return {"next": "call_tool", "tool": tool, "arguments": [{"name": k, "value": v} for k, v in args.items()], "result": None}


def step_finish(**result):
    base = {"classification": "UNKNOWN", "confidence": 0.5, "root_cause": None, "root_cause_claim_ids": [],
            "classification_claim_ids": [], "claims": [], "findings": [],
            "flags": {"bounded_change": False, "cross_component": False, "release_blocker": False,
                      "special_hardware": False, "duplicate_of": None}, "missing_evidence": []}
    base.update(result)
    return {"next": "finish", "tool": None, "arguments": [], "result": base}


def test_investigator_tool_loop_and_allow_list(run_context) -> None:
    res = resolver_with(run_context, make_record("18234", "issues", "passt cpu", confirmed_through=run_context.cutoff))
    out = ScriptedOutbound({"investigator": [
        step_tool("delete_everything"),
        step_tool("get_issue", issue_id="18234"),
        step_finish(classification="DEPENDENCY"),
    ]})
    r = Investigator(out, res).run("investigate failure 18234")
    assert r.status == "complete" and r.result["classification"] == "DEPENDENCY"
    assert r.refused_tools == ["delete_everything"] and r.tool_calls == 1 and r.records_read == ["18234"]
    # tool results are user-turn data, never in the system prompt
    last = out.calls[-1][1]
    assert "passt cpu" not in last["system"] and "passt cpu" in json.dumps(last["messages"])


def test_no_work_admitted_after_first_exhausted_budget(run_context) -> None:
    res = resolver_with(run_context, make_record("1", "issues", "x", confirmed_through=run_context.cutoff))
    out = ScriptedOutbound({"investigator": [step_tool("get_issue", issue_id="1")] * 10})
    r = Investigator(out, res, budget=Budget(max_tool_calls=2)).run("t")
    assert r.status == "partial" and r.budget_exhausted == "tool_calls" and r.tool_calls == 2
    assert len(out.calls) == 2  # no model call is admitted once the tool budget is spent

    clock = FakeClock(datetime(2026, 7, 30, tzinfo=timezone.utc))

    class Slow(ScriptedOutbound):
        def call(self, role, purpose, payload):
            clock.advance(minutes=6)
            return super().call(role, purpose, payload)

    slow = Slow({"investigator": [step_tool("get_issue", issue_id="1")] * 10})
    r2 = Investigator(slow, res, clock=clock.now).run("t")
    assert r2.status == "partial" and r2.budget_exhausted == "time" and len(slow.calls) == 3

    out3 = ScriptedOutbound({"investigator": [step_tool("nope")] * 40})
    r3 = Investigator(out3, res, budget=Budget(max_steps=5)).run("t")
    assert r3.budget_exhausted == "steps" and len(out3.calls) == 5


def test_log_cap_and_filter() -> None:
    log = "\n".join([f"line {i}" for i in range(60_000)] + ["FAIL: test_x timed out"])
    out = filter_log(log)
    assert "log cut" in out and "FAIL: test_x" in out and "line 5" not in out


def test_tools_cover_the_planned_list() -> None:
    names = set(build_tools())
    assert {"get_pull_request", "get_changed_components", "get_failed_pipeline_jobs", "get_cluster_events",
            "get_blocking_vulnerabilities", "get_error_rate", "trace_dependency_impact", "get_issue",
            "get_issue_timeline", "search_issues"} <= names
