"""Evaluation preparation: planted findings, new-evidence data sets, blocker labelling, claim
checking by one person, the harness and the acceptance record. No model calls."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval import acceptance, checks, claim_check, gates, proxy_label
from eval.harness import Harness, RunCache, SpendCeilingReached, report_of
from eval.store import EvaluationStore
from prep.new_evidence import snapshots as ne_snapshots
from prep.planted import from_cases

ROOT = Path(__file__).resolve().parents[2]
CASES = ROOT / "data" / "snapshots" / "cases"
needs_cases = pytest.mark.skipif(not (CASES / "case-g01" / "records.jsonl").exists(), reason="example-case data sets not present")


def answers(*values):
    it = iter(values)
    return lambda prompt: next(it)


# ---------------------------------------------------------------- planted findings

@needs_cases
def test_planted_set_is_built_from_real_records():
    spec = json.loads(from_cases.SPEC.read_text(encoding="utf-8"))
    planted = from_cases.build(spec)
    assert len(planted["true"]) == 10 and len(planted["wrong"]) == 10
    for item in planted["true"] + planted["wrong"]:
        rows = from_cases.load_rows(CASES / item["snapshot_id"])
        ids = {r["record_id"] for r in rows}
        assert set(item["evidence_ids"]) <= ids, "a planted finding must cite a record that exists in its data set"
    cites = {w["cites"] for w in planted["wrong"]}
    assert cites == {"the issue of the case", "an unrelated commit"}
    committed = json.loads(checks.PLANTED.read_text(encoding="utf-8"))
    assert [w["id"] for w in committed["wrong"]] == [w["id"] for w in planted["wrong"]]


def test_planted_set_refuses_a_missing_issue(tmp_path):
    case = tmp_path / "case-g01"
    case.mkdir()
    (case / "records.jsonl").write_text("", encoding="utf-8")
    spec = {"cases": [{"case": "G01", "true": {"text": "t", "support": {"source_id": "1"}}, "wrong": {"text": "w"}}]}
    with pytest.raises(from_cases.PlantedError):
        from_cases.build(spec, tmp_path)


def test_serving_code_never_opens_the_planted_set():
    for path in (ROOT / "atlas").rglob("*.py"):
        assert "evaluator/planted" not in path.read_text(encoding="utf-8").replace("\\", "/"), path


# ---------------------------------------------------------------- new evidence

@needs_cases
def test_new_evidence_data_sets_add_one_marked_record(tmp_path):
    built = ne_snapshots.build(CASES, tmp_path)
    assert len(built) == 5
    for item in built:
        folder = tmp_path / item["snapshot_id"]
        manifest = json.loads((folder / "snapshot.json").read_text(encoding="utf-8"))
        rows = [json.loads(line) for line in (folder / "records.jsonl").read_text(encoding="utf-8").splitlines()]
        changed = [r for r in rows if r["record_id"] == item["changed_record"]]
        assert len(changed) == 1 and changed[0]["provenance"] == "synthetic" and changed[0]["synthetic_ancestry"] == 1
        comments = json.loads(changed[0]["content"])["comments"]
        assert comments[-1]["author"] == "person-synthetic"
        assert manifest["test_copy"] == "new_evidence" and manifest["records"] == len(rows)
        base = from_cases.load_rows(CASES / f"case-{item['base_case'].lower()}")
        assert len(rows) == len(base), "exactly one record changes; none is added or removed"
        assert item["changed_record"] not in {r["record_id"] for r in base}


def test_new_evidence_copies_are_listed_as_test_copies():
    from atlas.api.snapshot_labels import is_test_copy, kind_of
    from atlas.snapshots import SnapshotRegistry

    assert is_test_copy({"kind": "new_evidence"}) and kind_of({"kind": "new_evidence"}) == "case"
    assert "new_evidence" in SnapshotRegistry(ROOT / "data").prepared_dirs


# ---------------------------------------------------------------- blocker labelling

def candidate(release="1.6", issue="1"):
    return {"release": release, "issue_id": issue, "reason": "critical-urgent and open at the release", "title": "t",
            "labels": ["kind/bug"], "body": "", "created_at": "", "open_at_previous_release": True, "fix_titles": [],
            "link": ""}


def test_candidates_come_from_the_release_outcome_files():
    found = proxy_label.load_candidates()
    assert {c["release"] for c in found} == set(proxy_label.RELEASES)
    assert all(c["reason"] for c in found)
    assert len({(c["release"], c["issue_id"]) for c in found}) == len(found)


def test_labelling_is_resumable_and_done_by_one_person(tmp_path):
    out = tmp_path / "labels.csv"
    cands = [candidate(issue=str(n)) for n in (1, 2, 3)]
    shown = []
    first = proxy_label.label(cands, out, answers("y", "note a", "s", "q"), shown.append)
    assert first == {"labelled": 1, "skipped": 1, "already": 0}
    state = proxy_label.blockers(out, cands)
    assert state["blockers"] == {"1.6": ["1"]} and state["missing"] == 2 and not state["complete"]
    second = proxy_label.label(cands, out, answers("n", "", "y", ""), shown.append)
    assert second["labelled"] == 2 and second["already"] == 1
    state = proxy_label.blockers(out, cands)
    assert state["complete"] and state["checked_by"] == "one person"
    assert state["blockers"] == {"1.6": ["1", "3"]}
    assert "[2 of 3]" in shown[1]


def test_provisional_labels_are_never_a_complete_result(tmp_path):
    out = tmp_path / "provisional.csv"
    cands = [candidate(issue="1"), candidate(issue="2")]
    assert proxy_label.write_provisional(cands, out) == 2
    state = proxy_label.blockers(out, cands)
    assert not state["complete"] and "provisional" in state["checked_by"]


def test_committed_label_file_holds_no_machine_labels():
    assert all(r["checked_by"] == proxy_label.CHECKED_BY for r in proxy_label.read_labels())


# ---------------------------------------------------------------- claim checking

def claim(cid, confidence, rejected=0, verdict="supported"):
    return {"claim_id": cid, "run": "R1 G03", "run_id": "run-1", "report_type": "investigation", "text": f"claim {cid}",
            "verdict": verdict, "output_version": "1", "evidence_versions": ["v1"], "confidence": confidence,
            "rejected_in_run": rejected, "evidence": [{"record_id": "r1", "text": "record text"}]}


def test_least_sure_claims_come_first():
    ordered = claim_check.order([claim("a", 0.9), claim("b", 0.4), claim("c", 0.4, rejected=2), claim("d", None)])
    assert [c["claim_id"] for c in ordered] == ["d", "c", "b", "a"]


def test_claim_checking_resumes_and_shows_progress(tmp_path):
    out = tmp_path / "claim_checks.csv"
    claims = [claim("a", 0.5), claim("b", 0.6), claim("c", 0.7)]
    shown = []
    assert claim_check.check(claims, out, answers("y", "", "q"), shown.append)["checked"] == 1
    pending = claim_check.claim_support(claims, claim_check.read_checks(out))
    assert pending["status"] == "unevaluated" and pending["counts"]["still_to_check"] == 2
    assert claim_check.check(claims, out, answers("y", "", "y", ""), shown.append)["already"] == 1
    assert "[2 of 3]" in shown[-2] and "[3 of 3]" in shown[-1]
    done = claim_check.claim_support(claims, claim_check.read_checks(out))
    assert done["status"] == "passed" and done["checked_by"] == "one person"


def test_one_unsupported_claim_fails_the_check(tmp_path):
    out = tmp_path / "claim_checks.csv"
    claims = [claim("a", 0.5), claim("b", 0.6)]
    claim_check.check(claims, out, answers("y", "", "n", "the record says the opposite"))
    failed = claim_check.claim_support(claims, claim_check.read_checks(out))
    assert failed["status"] == "failed" and failed["counts"]["unsupported"] == 1
    assert claim_check.claim_support([claim("z", 0.5, verdict="rejected")], [])["status"] == "failed"


def test_a_check_for_an_older_version_does_not_count(tmp_path):
    out = tmp_path / "claim_checks.csv"
    claim_check.check([claim("a", 0.5)], out, answers("y", ""))
    newer = dict(claim("a", 0.5), evidence_versions=["v2"])
    assert claim_check.claim_support([newer], claim_check.read_checks(out))["counts"]["still_to_check"] == 1


def test_collect_reads_claims_and_cited_text():
    runs = {"investigate|case-g03|17388|R1": {"run_id": "run-9", "reports": {"investigation": {
        "report_version": 2, "confidence": 0.6, "verifier": {"claims_rejected": 1},
        "claims": [{"id": "c1", "text": "the test failed", "evidence_ids": ["rec/1"], "verdict": "supported"}]}}}}
    got = claim_check.collect(runs, lambda run_id, record_id: {"content": "log line", "content_version_id": "cv9"})
    assert got[0]["claim_id"] == "run-9:investigation:c1" and got[0]["evidence"][0]["text"] == "log line"
    assert got[0]["evidence_versions"] == ["cv9"] and got[0]["output_version"] == "2"


# ---------------------------------------------------------------- harness

def fake_call(cost=0.5):
    calls = []

    def call(method, path, role, body=None):
        calls.append((method, path))
        if method == "POST":
            return 202, {"run_id": f"run-{len(calls)}"}
        if path.startswith("/api/runs/"):
            return 200, {"run_status": "complete", "admission_status": "done", "cost": {"usd": cost},
                         "reports": [{"report_id": "rep-1", "report_type": "investigation"}]}
        return 200, {"report": {"classification": {"value": "CODE_DEFECT"}, "action": {"value": "ASSIGN_DEVELOPER"}}}

    return call, calls


def test_harness_keeps_runs_and_does_not_pay_twice(tmp_path):
    call, calls = fake_call()
    h = Harness(call, session_dir=tmp_path, poll_seconds=0)
    first = h.investigate("case-g03", "17388")
    posts = sum(1 for m, _ in calls if m == "POST")
    again = Harness(call, session_dir=tmp_path, poll_seconds=0).investigate("case-g03", "17388")
    assert again == first and sum(1 for m, _ in calls if m == "POST") == posts
    assert report_of(first, "investigation")["action"]["value"] == "ASSIGN_DEVELOPER"


def test_harness_stops_at_the_spend_ceiling(tmp_path):
    call, _ = fake_call(cost=0.6)
    h = Harness(call, session_dir=tmp_path, max_usd=1.0, poll_seconds=0)
    h.investigate("case-g03", "1")
    h.investigate("case-g03", "2")
    with pytest.raises(SpendCeilingReached):
        h.investigate("case-g03", "3")
    assert RunCache(tmp_path / "runs.json").spend() == pytest.approx(1.2)


# ---------------------------------------------------------------- comparison with the simple workflow

@needs_cases
def test_workflow_reads_one_frozen_data_set():
    golden = checks.golden_expected()
    cases = checks.comparison_cases(golden)
    assert len(cases) == 12
    g06 = next(c for c in cases if c["id"] == "G06")
    inputs = checks.workflow_case(g06["snapshot_dir"], g06["issues"])
    assert inputs["failure_ids"] == ["17371"] and inputs["first_failure_at"]
    rows = checks.run_comparison({"G06": {"cost": {"tool_calls": 9}, "reports": {"investigation": {
        "classification": {"value": "ENVIRONMENT"}, "action": {"value": "HAND_TO_INFRA_OWNER"}}}}}, [g06])
    assert rows[0]["atlas_correct"] and rows[0]["atlas_cost"] == 9 and rows[0]["workflow_cost"] >= 1


def test_golden_expected_lists_ten_cases():
    golden = checks.golden_expected()
    assert list(golden) == list(gates.GOLDEN_IDS)
    assert sum(1 for g in golden.values() if g["kind"] == "investigation") == 7


# ---------------------------------------------------------------- acceptance plan and record

def test_plan_states_runs_cost_and_the_person_steps():
    full = acceptance.plan()
    assert full["runs"]["investigation"] == 7 * 6 + 5 and full["runs"]["release_close"] == 6 + 4
    assert full["expected_usd"] > 5 and full["checked_by"] == "one person"
    assert [h["step"] for h in full["human_steps"]] == ["check published claims", "label blocker candidates"]
    sample = acceptance.plan(sample=2)
    assert sample["sample"] and sample["runs"] == {"investigation": 2, "planted": 1} and not sample["human_steps"]


def report(cls, act, **extra):
    return {"classification": {"value": cls}, "action": {"value": act}, "report_version": 1, "confidence": 0.7,
            "verifier": {"claims_rejected": 0}, "claims": [], **extra}


def sample_session(tmp_path):
    golden = checks.golden_expected()
    steps = acceptance.golden_steps(golden, ("R1",), ("G03", "G07"))
    runs = {
        "investigate|case-g03|17388|R1": {"run_id": "run-a", "run_status": "complete",
                                         "cost": {"input_tokens": 1, "output_tokens": 1, "usd": 0.2, "steps": 4, "tool_calls": 6, "seconds": 60},
                                         "reports": {"investigation": report("CODE_DEFECT", "ASSIGN_DEVELOPER")}},
        "investigate|case-g07|17022|R1": {"run_id": "run-b", "run_status": "complete",
                                         "cost": {"input_tokens": 1, "output_tokens": 1, "usd": 0.2, "steps": 4, "tool_calls": 6, "seconds": 60},
                                         "reports": {"investigation": report("CODE_DEFECT", "ASSIGN_DEVELOPER")}},
    }
    (tmp_path / "plan.json").write_text(json.dumps({"sample": 2, "repetitions": 1, "steps": steps, "model_spend_usd": 0.4}))
    (tmp_path / "runs.json").write_text(json.dumps(runs))
    (tmp_path / "planted.json").write_text(json.dumps({"gate": gates.result("passed", {"planted_caught": 10, "controls_supported": 10})}))
    (tmp_path / "claims.json").write_text("[]")
    return tmp_path


@needs_cases
def test_a_sample_record_claims_nothing(tmp_path):
    session = sample_session(tmp_path)
    rec = acceptance.build_record(session, labels_path=tmp_path / "no-labels.csv")
    assert rec["overall"] == "sample" and rec["sample"] and "Not an evaluation result" in rec["label"]
    g = rec["gates"]
    assert g["G-1"]["status"] == "unevaluated" and "1 of 2 example cases correct" in g["G-1"]["failures"][0]
    assert g["G-2"]["status"] == "passed" and g["G-7"]["status"] == "passed"
    for gate_id in ("G-3", "G-4", "G-5", "G-6", "G-8", "G-9", "G-10", "G-11", "G-12", "G-13"):
        assert g[gate_id]["status"] == "unevaluated", gate_id
    assert "still to be labelled by a person" in g["G-13"]["failures"][0] and g["G-13"]["limitation"]
    assert set(g) == {f"G-{n}" for n in range(1, 14)}


@needs_cases
def test_a_sample_record_is_stored_but_is_never_the_latest_evaluation(tmp_path):
    session = sample_session(tmp_path)

    class Services:
        evaluations = EvaluationStore()

    h = Harness(lambda *a: (200, {}), services=Services())
    evaluation_id = acceptance.record(h, session)
    stored = Services.evaluations.get(evaluation_id)
    assert stored["status"] == "sample" and stored["sample"] is True
    assert Services.evaluations.latest() is None
    assert [e["evaluation_id"] for e in Services.evaluations.list()] == [evaluation_id]
    with pytest.raises(PermissionError):
        Services.evaluations.update(evaluation_id)


def test_gate_view_says_who_checked():
    from atlas.api.routes_eval import _gate_view

    view = _gate_view("G-3", gates.result("passed", {"claims": 2}, [], checked_by="one person"))
    assert view["summary"] == "Checked by one person"
    noted = _gate_view("G-5", dict(gates.result("passed"), note="timing only"))
    assert noted["summary"] == "timing only"
