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
