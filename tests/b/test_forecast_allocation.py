"""Every forecast item carries a proposed owner type, never a person."""

from datetime import datetime, timezone

from atlas.contracts import OpenItem
from atlas.services.forecast import forecast, propose_owner

AT = datetime(2026, 7, 30, tzinfo=timezone.utc)
OLD = datetime(2025, 1, 1, tzinfo=timezone.utc)


def item(issue: str, title: str, labels: set[str], component: str | None = "compute", fan_in: int = 0) -> OpenItem:
    return OpenItem(issue_id=issue, title=title, labels=frozenset(labels), created=OLD, last_activity=OLD,
                    component=component, record_ids=(f"issues:{issue}@v1",), fan_in=fan_in)


def test_owner_rules():
    assert propose_owner(item("1", "flaky test", {"kind/flake", "priority/critical-urgent"}))[0] == "decision_maker"
    assert propose_owner(item("2", "eviction test on S390X", {"kind/flake"}))[0] == "developer"
    assert propose_owner(item("3", "flaky test in one package", {"kind/flake"}))[0] == "coding_agent"
    assert propose_owner(item("4", "bug", {"kind/bug"}))[0] == "developer"
    assert propose_owner(item("5", "flaky", {"kind/flake"}, component=None))[0] == "developer"
    assert propose_owner(item("6", "flaky", {"kind/flake"}, fan_in=9))[0] == "developer"


def test_forecast_items_carry_a_proposal():
    out = forecast([item("3", "flaky test in one package", {"kind/flake"}), item("4", "bug", {"kind/bug"})], AT)
    assert out["items"], "old items must be ranked"
    for it in out["items"]:
        assert it["allocation"] in {"decision_maker", "developer", "coding_agent"}
        assert it["allocation_reason"]
        assert it["allocation_basis"] == "proposal"
