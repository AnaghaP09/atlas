"""Forecast accuracy counts only blockers already open when the forecast was made. No model calls."""

from __future__ import annotations

import json
from pathlib import Path

from eval import proxy_label
from eval.canonical import NOTE
from prep.proxy import build

ROOT = Path(__file__).resolve().parents[2]


def issue_row(number, state, valid_from, seq=1, status="current"):
    return {"entity_type": "issue", "status": status, "valid_from": valid_from, "seq": seq,
            "content": json.dumps({"number": number, "state": state, "title": f"issue {number}", "labels": ["kind/bug"]})}


def write_data_set(root: Path, name: str, cutoff: str, rows: list[dict]) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "snapshot.json").write_text(json.dumps({"cutoff": cutoff}), encoding="utf-8")
    (d / "records.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return d


CUTOFF = "2025-03-13T18:00:00+00:00"


def outcome(candidates):
    return {"release": "1.6", "window": {"forecast_cutoff": CUTOFF}, "backports": [], "candidates": candidates}


def cand(number, created="2024-01-01T00:00:00Z", open_prev=True):
    return {"issue": number, "created_at": created, "open_at_previous_close": open_prev,
            "reasons": [{"kind": "critical_open_at_final_tag"}], "kinds_now": ["kind/bug"]}


def test_forecast_view_follows_the_as_of_rules(tmp_path):
    d = write_data_set(tmp_path, "release-close-v1.5.0", CUTOFF, [
        issue_row(1, "OPEN", "2024-01-01T00:00:00Z"),
        issue_row(2, "OPEN", "2024-01-01T00:00:00Z"), issue_row(2, "CLOSED", "2025-01-01T00:00:00Z", seq=2),
        issue_row(3, "OPEN", "2025-04-01T00:00:00Z"),  # after the cutoff: invisible
        issue_row(4, "OPEN", "2024-01-01T00:00:00Z", status="superseded"),
        {"entity_type": "component", "status": "current", "valid_from": "2024-01-01T00:00:00Z", "content": "{}"},
    ])
    assert build.forecast_view(d) == {"1": "OPEN", "2": "CLOSED"}
    assert build.forecast_view(tmp_path / "missing") is None


def test_only_candidates_open_at_forecast_reach_the_queue_and_the_rest_keep_a_reason():
    view = {"1": "OPEN", "2": "CLOSED"}
    new, left = build.split_outcome(outcome([
        cand(1), cand(2, open_prev=False), cand(3, created="2025-05-01T00:00:00Z", open_prev=False), cand(4)]), view)
    assert [c["issue"] for c in new["candidates"]] == [1]
    assert all(c["open_at_forecast"] is True for c in new["candidates"])
    why = {c["issue"]: c["why_left_out"] for c in left}
    assert why == {2: build.CLOSED_THEN, 3: build.REPORTED_LATER, 4: build.NOT_IN_DATA_SET}
    assert all(c["open_at_forecast"] is False and c["release"] == "1.6" for c in left)
    assert new["eligibility"]["forecast_data_set"] == "release-close-v1.5.0"
    assert (new["eligibility"]["open_at_forecast"], new["eligibility"]["not_open_at_forecast"]) == (1, 3)


def test_without_the_forecast_data_set_nothing_is_eligible():
    new, left = build.split_outcome(outcome([cand(1)]), None)
    assert new["candidates"] == [] and "not present" in left[0]["why_left_out"]


def test_rebuild_is_repeatable_and_feeds_the_labelling_tool(tmp_path):
    releases = tmp_path / "releases"
    releases.mkdir()
    write_data_set(releases, "release-close-v1.5.0", CUTOFF, [issue_row(1, "OPEN", "2024-01-01T00:00:00Z")])
    outcomes = tmp_path / "outcomes"
    outcomes.mkdir()
    (outcomes / "1.6.json").write_text(json.dumps(outcome([cand(1), cand(9, created="2025-06-01T00:00:00Z")])),
                                       encoding="utf-8")
    first = build.rebuild_outcomes(outcomes, releases, tmp_path / "queue.csv")
    files = {p.name: p.read_bytes() for p in outcomes.iterdir()}
    second = build.rebuild_outcomes(outcomes, releases, tmp_path / "queue.csv")
    assert first == second == {"1.6": {"candidates": 2, "open_at_forecast": 1, "not_open_at_forecast": 1}}
    assert files == {p.name: p.read_bytes() for p in outcomes.iterdir()}
    found = proxy_label.load_candidates(outcomes, releases, ("1.6",))
    assert [c["issue_id"] for c in found] == ["1"] and found[0]["open_at_forecast"] is True
    assert "Open when the forecast was made: yes" in proxy_label.describe(found[0], 1, 1)
    side = proxy_label.left_out(outcomes / build.LEFT_OUT)
    assert [c["issue"] for c in side] == [9] and side[0]["why_left_out"] == build.REPORTED_LATER


def test_candidates_not_marked_open_are_never_queued(tmp_path):
    (tmp_path / "1.6.json").write_text(json.dumps(outcome([cand(1)])), encoding="utf-8")  # field missing
    assert proxy_label.load_candidates(tmp_path, tmp_path, ("1.6",)) == []


def test_recall_denominator_is_the_queue_and_precision_keeps_every_blocker(tmp_path):
    out = tmp_path / "labels.csv"
    queue = [{"release": "1.6", "issue_id": "1", "reason": "r"}, {"release": "1.6", "issue_id": "2", "reason": "r"}]
    for issue, final in (("1", "blocker"), ("2", "not_blocker"), ("5", "blocker")):  # 5 is an older label, not queued
        proxy_label._append(out, {"release": "1.6", "issue_id": issue, "final": final,
                                  "checked_by": proxy_label.CHECKED_BY})
    state = proxy_label.blockers(out, queue)
    assert state["blockers"] == {"1.6": ["1"]} and state["all_blockers"] == {"1.6": ["1", "5"]}
    assert state["labelled"] == 2 and state["candidates"] == 2 and state["complete"]


def test_the_tool_shows_the_new_count_and_never_labels_on_its_own(tmp_path, capsys, monkeypatch):
    labels = tmp_path / "labels.csv"
    assert proxy_label.main(["--status", "--out", str(labels)]) == 0
    text = capsys.readouterr().out
    queued, left = len(proxy_label.load_candidates()), len(proxy_label.left_out())
    assert f"{queued} candidates were open when the forecast was made" in text and f"{left} were not" in text
    assert NOTE in text and not labels.exists()


def test_committed_outcomes_hold_only_candidates_open_at_forecast():
    queued = proxy_label.load_candidates()
    side = proxy_label.left_out()
    assert queued and side
    for release in proxy_label.RELEASES:
        data = json.loads((proxy_label.OUTCOMES / f"{release}.json").read_text(encoding="utf-8"))
        assert all(c["open_at_forecast"] is True for c in data["candidates"])
        assert data["eligibility"]["open_at_forecast"] == len(data["candidates"])
        assert data["eligibility"]["forecast_data_set"] == build.previous_close(release)
    assert all(c["open_at_forecast"] is False and c["why_left_out"] for c in side)
    assert not {(c["release"], c["issue_id"]) for c in queued} & {(c["release"], str(c["issue"])) for c in side}
