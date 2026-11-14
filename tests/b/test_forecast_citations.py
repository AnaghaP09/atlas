"""A release close must verify forecast items whose citations are full record ids.

Real records have ids such as ``issues:issue/18236@<version>`` that differ from their source id
(``issue/18236``). The forecast cites record ids, so the evidence must be loaded by record id;
otherwise every claim is rejected as "not in the run's evidence" and the report is empty.
"""

from __future__ import annotations

from datetime import timedelta

from atlas.contracts import StartedBy, StartedByKind
from atlas.services.forecast import OpenItem
from tests.b.helpers import ScriptedOutbound, make_record
from tests.b.test_orchestrator import build, verdicts


def _item(run_context, number: str) -> OpenItem:
    at = run_context.evidence_clock
    return OpenItem(number, f"Flaky test {number}", frozenset({"priority/critical-urgent", "lifecycle/stale"}),
                    at - timedelta(days=200), at - timedelta(days=150), "compute",
                    (f"issues:issue/{number}@v1",), ci_recurrences=5)


def test_forecast_items_cited_by_record_id_reach_sentinel(run_context) -> None:
    items = [_item(run_context, "18236"), _item(run_context, "18020")]
    records = [make_record(f"issues:issue/{i.issue_id}@v1", "issues", i.title, source_id=f"issue/{i.issue_id}",
                           confirmed_through=run_context.cutoff) for i in items]
    s = ScriptedOutbound({"verifier": [verdicts(**{"fc-1": "supported", "fc-2": "supported"})]})
    orch, _ = build(run_context, s, records=records)
    orch.d.open_items_for = lambda ctx: items
    out = orch.run("run-fc", run_context, {"trigger": "release_close", "release_id": "v1.9.0"},
                   StartedBy(StartedByKind.WATCHER, watcher_id="releases"))
    assert [i["issue_id"] for i in out["forecast"]["items"]] == ["18236", "18020"]
    assert out["verifier"]["claims_rejected"] == 0
    assert all(c["verdict"] == "supported" for c in out["claims"])


def test_a_citation_that_does_not_match_the_stored_version_is_still_rejected(run_context) -> None:
    item = _item(run_context, "18236")
    stale = make_record("issues:issue/18236@v0", "issues", item.title, source_id="issue/18236",
                        confirmed_through=run_context.cutoff)
    s = ScriptedOutbound({"verifier": [verdicts(**{"fc-1": "supported"})]})
    orch, _ = build(run_context, s, records=[stale])
    orch.d.open_items_for = lambda ctx: [item]
    out = orch.run("run-fc2", run_context, {"trigger": "release_close", "release_id": "v1.9.0"},
                   StartedBy(StartedByKind.WATCHER, watcher_id="releases"))
    assert out["forecast"]["items"] == []
    assert out["verifier"]["claims_rejected"] == 1
