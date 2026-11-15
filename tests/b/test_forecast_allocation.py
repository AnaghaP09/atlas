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
    assert propose_owner(item("2", "eviction test on S390X", {"kind/flake"}))[0] == "developer"
    assert propose_owner(item("3", "flaky test in one package", {"kind/flake"}))[0] == "coding_agent"
    assert propose_owner(item("4", "bug", {"kind/bug"}))[0] == "developer"
    assert propose_owner(item("5", "flaky", {"kind/flake"}, component=None))[0] == "developer"
    assert propose_owner(item("6", "flaky", {"kind/flake"}, fan_in=9))[0] == "developer"


def test_priority_alone_is_not_a_decision():
    """An urgent label raises the score; the nature of the work decides the owner."""
    owner, reason = propose_owner(item("1", "flaky test", {"kind/flake", "priority/critical-urgent"}))
    assert owner == "coding_agent"
    assert 3 <= len(reason.split()) <= 6


def test_only_real_decisions_go_to_a_person():
    assert propose_owner(item("7", "crash on start", {"kind/bug", "release-blocker/main"}))[0] == "decision_maker"
    assert propose_owner(item("8", "allow old cipher", {"kind/bug", "needs-decision"}))[0] == "decision_maker"
    assert propose_owner(item("9", "urgent bug", {"kind/bug", "priority/critical-urgent"}))[0] == "developer"


def test_unclear_cause_and_large_changes_go_to_a_developer():
    assert propose_owner(item("10", "lane: clustered failures", {"kind/flake"}))[0] == "developer"
    assert propose_owner(item("11", "around 20 tests failing on every run", {"kind/flake"}))[0] == "developer"
    assert propose_owner(item("12", "new snapshot API", {"kind/enhancement"}))[0] == "developer"
    assert propose_owner(item("13", "flaky on arm", {"kind/flake", "wg/arch-arm64"}))[0] == "developer"


def test_bounded_changes_go_to_the_coding_agent():
    assert propose_owner(item("14", "Bump golang.org/x/net module version", {"kind/bug"}))[0] == "coding_agent"
    assert propose_owner(item("15", "nil check", {"kind/bug", "kind/failing-test"}))[0] == "coding_agent"


def test_every_reason_is_short():
    cases = [item("a", "x", {"release-blocker/main"}), item("b", "x", {"kind/flake"}, fan_in=6),
             item("c", "x", {"kind/flake"}, component=None), item("d", "on s390x", set()),
             item("e", "clustered failures", set()), item("f", "x", {"kind/feature"}), item("g", "x", {"kind/flake"}),
             item("h", "bump module version", set()), item("i", "x", {"kind/bug"})]
    for c in cases:
        words = propose_owner(c)[1].split()
        assert 3 <= len(words) <= 6, propose_owner(c)


def test_owner_team_comes_from_group_labels():
    from atlas.services.forecast import owner_team

    assert owner_team(item("1", "x", {"sig/compute", "kind/flake"})) == "sig/compute"
    assert owner_team(item("2", "x", {"kind/flake"})) is None


def test_forecast_items_carry_a_proposal():
    out = forecast([item("3", "flaky test in one package", {"kind/flake"}), item("4", "bug", {"kind/bug"})], AT)
    assert out["items"], "old items must be ranked"
    for it in out["items"]:
        assert it["allocation"] in {"decision_maker", "developer", "coding_agent"}
        assert it["allocation_reason"]
        assert it["allocation_basis"] == "proposal"
