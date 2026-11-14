"""Tool results and the whole conversation are bounded, so one large record can never overflow the
model's context or run up cost (a long issue thread once produced a 1.5-million-token prompt)."""

from __future__ import annotations

import json

from atlas.agents.investigator import (
    CONVERSATION_MAX_CHARS,
    RESULT_MAX_CONTENT_CHARS,
    RESULT_MAX_RECORDS,
    RESULT_MAX_TOTAL_CHARS,
    Investigator,
    bound_result,
)
from tests.b.helpers import ScriptedOutbound, make_record
from tests.b.test_agents import resolver_with, step_tool


def test_bound_result_limits_records_content_and_total() -> None:
    data = [{"record_id": f"r{i}", "content": "x" * 10_000} for i in range(50)]
    out, notes = bound_result(data)
    assert len(out) <= RESULT_MAX_RECORDS
    assert all(len(d["content"]) <= RESULT_MAX_CONTENT_CHARS + 60 for d in out)
    assert len(json.dumps(out)) <= RESULT_MAX_TOTAL_CHARS + RESULT_MAX_CONTENT_CHARS + 200
    assert any("narrow" in n for n in notes)


def test_small_results_are_unchanged() -> None:
    data = [{"record_id": "r1", "content": "short"}]
    assert bound_result(data) == (data, [])


def test_scout_sees_a_cut_record_and_the_prompt_stays_small(run_context) -> None:
    huge = make_record("18234", "issues", "line of an issue thread\n" * 200_000, confirmed_through=run_context.cutoff)
    res = resolver_with(run_context, huge)
    out = ScriptedOutbound({"investigator": [step_tool("get_issue", issue_id="18234")] * 30})
    r = Investigator(out, res).run("investigate failure 18234")
    biggest = max(len(json.dumps(call[1]["messages"])) for call in out.calls)
    assert biggest < CONVERSATION_MAX_CHARS + RESULT_MAX_TOTAL_CHARS + 10_000
    assert r.status == "partial" and r.budget_exhausted in ("context", "steps", "tool_calls")
    assert "content cut" in json.dumps(out.calls[1][1]["messages"])


def test_a_single_record_may_use_the_whole_result() -> None:
    out, notes = bound_result([{"record_id": "r1", "content": "x" * 30_000}])
    assert 20_000 <= len(out[0]["content"]) <= 24_000
    assert any("read_record" in n for n in notes)
    small, no_notes = bound_result([{"record_id": "r1", "content": "x" * 15_000}])
    assert len(small[0]["content"]) == 15_000 and not no_notes


def test_log_excerpt_filters_the_log_field_of_a_stored_job_record() -> None:
    import json

    from atlas.agents.investigator import _log_excerpt

    log = "\n".join(["setup step ok"] * 500 + ["FAIL: [sig-network] test timed out"] + ["cleanup ok"] * 500)
    doc = json.loads(_log_excerpt(json.dumps({"job": "lane-a", "result": "FAILURE", "log": log}), None))
    assert doc["job"] == "lane-a" and doc["log_lines_total"] == 1001
    assert doc["log_excerpt"] == "FAIL: [sig-network] test timed out" and "log" not in doc
    only = json.loads(_log_excerpt(json.dumps({"job": "lane-a", "log": log}), "cleanup"))
    assert only["log_excerpt"].startswith("cleanup ok")
    assert _log_excerpt("plain\nFAIL here", None) == "FAIL here"


def test_read_record_returns_a_later_part_of_a_record_already_read(run_context) -> None:
    from atlas.agents.investigator import build_tools
    from atlas.agents.resolver import EvidenceResolver
    from tests.b.helpers import make_record
    from tests.fakes import InMemoryStore

    store = InMemoryStore()
    store.snapshot_for_write = run_context.snapshot_id
    store.put(make_record("log9", "ci", "a" * 100 + "TAIL"), "ci-adapter")
    res = EvidenceResolver(store, run_context)
    tools = build_tools()
    assert tools["read_record"].fn(res, {"record_id": "log9", "offset": "100"}) == []  # not read yet
    first = res.get("ci", "log9")
    more = tools["read_record"].fn(res, {"record_id": first.record_id, "offset": "100"})
    assert more[0].content.endswith("TAIL") and "from character 100" in more[0].content
    assert res.by_id(first.record_id).content == "a" * 100 + "TAIL"  # the stored record is unchanged
