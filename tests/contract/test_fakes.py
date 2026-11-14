"""The fakes honour the shared interfaces and their key rules."""

from __future__ import annotations

from datetime import timedelta

import pytest

from atlas import contracts
from atlas.contracts import EgressPurpose, Principal, Role
from tests.fakes import (
    FakeApprovals,
    FakeConnector,
    FakeContainment,
    FakeNotifier,
    FakeOutbound,
    FakeProjection,
    FakeTool,
    InMemoryStore,
    RecordedModel,
)


def test_fakes_satisfy_protocols(store, outbound, approvals, projection, notifier, containment, clock) -> None:
    assert isinstance(store, contracts.Store)
    assert isinstance(outbound, contracts.Outbound)
    assert isinstance(approvals, contracts.Approvals)
    assert isinstance(projection, contracts.Projection)
    assert isinstance(notifier, contracts.Notifier)
    assert isinstance(containment, contracts.Containment)
    assert isinstance(clock, contracts.Clock)
    assert isinstance(FakeConnector("issues"), contracts.Connector)
    assert isinstance(FakeTool("get_issue"), contracts.Tool)


def test_void_superseded_voids_lower_generations_only() -> None:
    a = FakeApprovals()
    old_a = a.add("run-1", 1, "A")
    old_b = a.add("run-1", 1, "B")
    current = a.add("run-1", 2, "A")
    other = a.add("run-2", 1, "A")
    assert set(a.void_superseded("run-1", new_generation=2)) == {old_a, old_b}
    assert a.void_superseded("run-1", new_generation=2) == []
    assert {r.approval_id for r in a.rows if r.voided} == {old_a, old_b}
    assert current and other


def test_projection_withholds_missing_and_unknown_scopes() -> None:
    artifact = {
        "findings": [
            {"text": "public", "claim_ids": [], "required_scopes": ["public"]},
            {"text": "restricted", "claim_ids": [], "required_scopes": ["security"]},
            {"text": "unknown", "claim_ids": [], "required_scopes": None},
        ]
    }
    pm = Principal(role=Role.PM, scopes=frozenset({"public"}))
    out = FakeProjection().project(artifact, pm)
    assert [f["text"] for f in out["findings"]] == ["public"]
    assert out["restricted_items"] == 2
    security = Principal(role=Role.SECURITY, scopes=frozenset({"public", "security"}))
    assert [f["text"] for f in FakeProjection().project(artifact, security)["findings"]] == ["public", "restricted"]


def test_notifier_takes_severity_from_catalog() -> None:
    n = FakeNotifier()
    n.emit("spend.80", "budget", [], frozenset())
    assert n.records[0]["severity"] == "warning"
    assert set(n.records[0]["roles"]) == {"em", "audit"}
    with pytest.raises(KeyError):
        n.emit("not.an.event", "x", [], frozenset())


def test_offline_outbound_refuses_and_counts() -> None:
    o = FakeOutbound(offline=True)
    with pytest.raises(ConnectionRefusedError):
        o.call("investigator", EgressPurpose.MODEL_CALL, {"q": 1})
    assert o.attempts == [("investigator", EgressPurpose.MODEL_CALL)]


def test_recorded_model_refuses_unknown_requests() -> None:
    m = RecordedModel()
    m.record({"q": 1}, {"text": "a"})
    assert m.respond({"q": 1}) == {"text": "a"}
    with pytest.raises(KeyError):
        m.respond({"q": 2})


def test_store_single_writer_and_snapshot_isolation(run_context) -> None:
    from datetime import datetime, timezone

    from atlas.contracts import Provenance, Record

    t = datetime(2026, 7, 1, tzinfo=timezone.utc)
    rec = Record("r1", "issues", "18234", 1, "v1", t, t, t, "issues-adapter", Provenance.OBSERVED, False,
                 frozenset({"public"}), (), "blob:1")
    s = InMemoryStore(writers={"issues": "issues-adapter"})
    with pytest.raises(PermissionError):
        s.put(rec, writer="someone-else")
    s.snapshot_for_write = run_context.snapshot_id
    s.put(rec, writer="issues-adapter")
    assert s.get(run_context, "issues", "18234") is not None
    other = type(run_context)(**{**run_context.__dict__, "snapshot_id": "snap-other"})
    assert s.get(other, "issues", "18234") is None


def test_containment_blocks_consumer(containment) -> None:
    containment.trip("publication", "diff changed after approval")
    assert not containment.permits("publication").allowed
    assert containment.permits("model_calls").allowed
    containment.resume("publication")
    assert containment.permits("publication").allowed


def test_clock_advances(clock) -> None:
    start = clock.now()
    clock.advance(hours=25)
    assert clock.now() - start == timedelta(hours=25)
