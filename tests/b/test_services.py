from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from atlas.services.allocation import Flags, allocate, simulate_staffing
from atlas.services.disposition import FeedbackStore, apply_injection_disposition
from atlas.services.forecast import OpenItem, forecast

AT = datetime(2026, 7, 30, tzinfo=timezone.utc)


def item(iid, title, labels=(), age=120, stale=100, ci=0, **kw):
    return OpenItem(iid, title, frozenset(labels), AT - timedelta(days=age), AT - timedelta(days=stale), "compute",
                    (f"rec-{iid}",), ci_recurrences=ci, **kw)


def test_forecast_excludes_trackers_and_marks_illustrative() -> None:
    items = [
        item("18236", "Flaky: eviction strategy test", ["priority/critical-urgent", "lifecycle/stale", "kind/flake"], ci=5),
        item("18020", "Flake: TPM migration test", ["kind/flake"], ci=4, age=60, stale=50),
        item("15907", "flaky tests: USB passthrough", ["priority/critical-urgent", "lifecycle/stale"], age=280),
        item("17720", "[sig-compute] Quarantine Transition Tracker", ["lifecycle/frozen", "kind/flake"], ci=9, age=90),
        item("19000", "new minor flake", ["kind/flake"], age=2, stale=1),
    ]
    f = forecast(items, AT)
    ids = [i["issue_id"] for i in f["items"]]
    assert {"18236", "18020", "15907"} <= set(ids) and "17720" not in ids and "19000" not in ids
    assert f["excluded_trackers"] == ["17720"]
    assert all(i["probability_range"] == "illustrative" for i in f["items"]) and f["date_range"] == "illustrative"
    assert {c["id"] for c in f["claims"]} == {cid for i in f["items"] for cid in i["claim_ids"]}


def test_requirements_at_risk_flag() -> None:
    f = forecast([item("1", "x", ["priority/critical-urgent"], ci=5, requirement_bound=True)], AT)
    assert f["requirements_at_risk"] == ["1"]


@pytest.mark.parametrize(
    "cls,flags,expected",
    [
        ("DEPENDENCY", Flags(bounded_change=True), "DELEGATE_TO_CODING_AGENT"),
        ("TEST_DEFECT", Flags(bounded_change=True), "DELEGATE_TO_CODING_AGENT"),
        ("CODE_DEFECT", Flags(), "ASSIGN_DEVELOPER"),
        ("CODE_DEFECT", Flags(release_blocker=True), "ESCALATE_DECISION"),
        ("ENVIRONMENT", Flags(), "HAND_TO_INFRA_OWNER"),
        ("DUPLICATE", Flags(duplicate_of="17019"), "LINK_DUPLICATE"),
        ("UNKNOWN", Flags(), "REJECT"),
        ("OUT_OF_SCOPE", Flags(), "REJECT"),
        ("CARRY_OVER_FORECAST", Flags(), "PUBLISH_FORECAST"),
        ("DEPENDENCY", Flags(bounded_change=True, cross_component=True), "ASSIGN_DEVELOPER"),
        ("CODE_DEFECT", Flags(special_hardware=True), "ASSIGN_DEVELOPER"),
    ],
)
def test_allocation_table(cls, flags, expected) -> None:
    assert allocate(cls, flags) == expected


def test_simulate_staffing_three_options_no_names_no_dates() -> None:
    alloc = [
        {"id": "a", "action": "DELEGATE_TO_CODING_AGENT", "skills": ["go"], "effort": 1},
        {"id": "b", "action": "ASSIGN_DEVELOPER", "skills": ["storage"], "effort": 2},
        {"id": "c", "action": "ASSIGN_DEVELOPER", "skills": ["s390x"], "effort": 1},
    ]
    cap = [{"role_id": "dev-1", "role": "developer", "skills": ["go", "storage"], "available": 2}]
    s = simulate_staffing(alloc, cap)
    assert len(s["options"]) >= 3 and s["dates"] is None and s["data_mode"] == "SYNTHETIC"
    assert "c" in s["options"][0]["unassigned"]
    assert s["options"][1]["load_per_role"]["coding_agent"] == 1
    with pytest.raises(ValueError):
        simulate_staffing(alloc, [{"role_id": "x", "role": "dev", "skills": [], "available": 1, "name": "Jane"}])


def test_injection_disposition_holds_and_strips_tasks() -> None:
    a = {"action": {"value": "DELEGATE_TO_CODING_AGENT", "claim_ids": ["c1"], "required_scopes": ["public"]},
         "options": [{"text": "Prepare a task to bump passt", "claim_ids": []}, {"text": "Read the log", "claim_ids": []}],
         "claims": [{"id": "c1", "kind": "task_rationale"}, {"id": "c2", "kind": "finding"}], "task": {"x": 1}}
    out = apply_injection_disposition(a, True)
    assert out["evidence_recommendation"] == "DELEGATE_TO_CODING_AGENT"
    assert out["action"]["value"] == "REJECT" and out["safety_disposition"] == "HOLD_FOR_HUMAN"
    assert "task" not in out and [o["text"] for o in out["options"]] == ["Read the log"]
    assert [c["id"] for c in out["claims"]] == ["c2"]
    clean = apply_injection_disposition(a, False)
    assert clean["action"]["value"] == "DELEGATE_TO_CODING_AGENT" and clean["safety_disposition"] == "NONE"


def test_feedback_written_with_lineage_and_append_only() -> None:
    fs = FeedbackStore()
    rec = fs.record_feedback("run-1", "override", "item-1", "assign developer", "needs hardware", "em")
    assert rec["lineage"] == ["run-1", "item-1"] and rec["golden_candidate"]
    assert fs.golden_candidates()[0]["id"] == rec["id"]
    fs.record_feedback("run-1", "debt_acceptance", "item-2", "accept", "low impact", "architect")
    assert len(fs.golden_candidates()) == 1
    with pytest.raises(PermissionError):
        fs.update(rec["id"])
    with pytest.raises(ValueError):
        fs.record_feedback("run-1", "override", "i", "d", " ", "em")
