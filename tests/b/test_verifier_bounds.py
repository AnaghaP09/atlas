"""Sentinel sees bounded record text, so a long cited record cannot overflow the model's context."""

from __future__ import annotations

import json

from atlas.agents.verifier import CUT_NOTE, RECORDS_MAX_TOTAL_CHARS, RECORD_MAX_CHARS, Verifier
from tests.b.helpers import ScriptedOutbound, make_record
from tests.b.test_agents import resolver_with


def test_cited_records_are_cut_and_marked(run_context) -> None:
    big = [make_record(f"r{i}", "issues", "thread line\n" * 20_000, confirmed_through=run_context.cutoff) for i in range(8)]
    res = resolver_with(run_context, *big)
    claims = [{"id": f"c{i}", "text": "claim", "kind": "finding", "evidence_ids": [f"r{i}"], "depends_on": [],
               "required_scopes": ["public"], "verdict": "missing"} for i in range(8)]
    out = ScriptedOutbound({"verifier": [{"verdicts": [{"claim_id": f"c{i}", "verdict": "supported", "reason": "r",
                                                         "contradicting_record": None} for i in range(8)]}]})
    result = Verifier(out, res.by_id).verify(claims)
    sent = out.calls[0][1]["messages"][0]["content"]
    assert len(sent) < 2 * RECORDS_MAX_TOTAL_CHARS  # JSON escaping can double newline-heavy text
    assert CUT_NOTE in sent and len(sent) < 8 * RECORD_MAX_CHARS
    assert result.claims_rejected == 0
