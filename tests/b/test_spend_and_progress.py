from __future__ import annotations

import pytest

from atlas.contracts import EgressPurpose
from atlas.outbound import CeilingGate, OutboundBoundary, OutboundRefused, SpendMeter
from atlas.projection import principal_for
from tests.b.helpers import ScriptedOutbound, ScriptedTransport
from tests.b.test_agents import step_tool
from tests.b.test_orchestrator import ONCALL, TRIGGER, build, recs, finish_dependency, verdicts

PAYLOAD = {"system": "s", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 10}


def test_spend_notifies_at_80_and_100_then_refuses_model_calls() -> None:
    sent = []
    meter = SpendMeter(0.0005, notify=lambda e, usd: sent.append(e))
    b = OutboundBoundary(transport=ScriptedTransport([{}] * 5), spend=meter.add, containment=CeilingGate(meter))
    b.call("investigator", EgressPurpose.MODEL_CALL, PAYLOAD)  # 100 in / 20 out on Sonnet = 0.0004
    assert sent == ["spend.80"] and meter.hold_reason() is None
    b.call("investigator", EgressPurpose.MODEL_CALL, PAYLOAD)
    assert sent == ["spend.80", "spend.100"] and meter.hold_reason() == "spend ceiling reached"
    with pytest.raises(OutboundRefused, match="spend ceiling"):
        b.call("investigator", EgressPurpose.MODEL_CALL, PAYLOAD)
    meter.add(0.1)
    assert sent.count("spend.100") == 1


def test_scout_progress_events_per_tool_step(run_context) -> None:
    s = ScriptedOutbound({"investigator": [step_tool("get_issue", issue_id="18234"), finish_dependency()],
                          "verifier": [verdicts(c1="supported", c2="supported")]})
    orch, deps = build(run_context, s, records=recs(run_context))
    orch.run("run-p", run_context, TRIGGER, ONCALL)
    progress = [e for e in deps.events.since(None, principal_for("em")) if e["event"] == "run.progress" and e.get("agent") == "scout"]
    assert progress and progress[0]["data"]["tool"] == "get_issue"
