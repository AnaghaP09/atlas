"""Component churn in the carry-over forecast.

Churn is the number of commits that touched each component in the 120 days before the release.
It is scaled per owner team to 0..20 and adds at most ``CHURN_WEIGHT`` to an item's score, so an
issue in a busy area ranks above an otherwise equal issue in a quiet one.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from atlas.connectors import github_queries as Q
from atlas.contracts import OpenItem
from atlas.data_wiring import SnapshotOpenItems
from atlas.gateway import check_graphql
from atlas.services.forecast import CHURN_WEIGHT, forecast, score

AT = datetime(2026, 7, 30, tzinfo=timezone.utc)
CREATED = datetime(2026, 3, 1, tzinfo=timezone.utc)


def item(issue_id: str, component: str | None, churn: int = 0) -> OpenItem:
    return OpenItem(issue_id=issue_id, title=f"Flaky test {issue_id}", labels=frozenset({"kind/flake"}), created=CREATED,
                    last_activity=CREATED, component=component, record_ids=(f"issues:issue/{issue_id}@v1",),
                    component_churn=churn)


def test_churn_adds_at_most_its_weight():
    quiet, busy = item("1", "storage", 0), item("2", "compute", 20)
    assert score(busy, AT) - score(quiet, AT) == pytest.approx(CHURN_WEIGHT)
    assert score(item("3", "compute", 400), AT) == score(busy, AT)


def test_busier_component_ranks_first_and_churn_is_shown():
    out = forecast([item("1", "storage", 2), item("2", "compute", 20), item("3", "network", 10)], AT)
    assert [i["issue_id"] for i in out["items"]] == ["2", "3", "1"]
    assert [i["component_churn"] for i in out["items"]] == [20, 10, 2]


class FakeRecord:
    def __init__(self, record_id):
        self.record_id = record_id


class FakeStore:
    """Churn records for three components owned by two teams."""

    def __init__(self, rows):
        self.rows = rows

    def search(self, ctx, query):
        if query.get("entity_type") == "component_churn":
            return [FakeRecord(str(n)) for n in range(len(self.rows))]
        return []

    def content(self, ctx, record_id):
        return self.rows[int(record_id)]


def test_team_churn_is_summed_per_owner_team_and_scaled():
    store = FakeStore([
        {"component": "pkg/a", "owner_team": "sig/compute", "commits": 60},
        {"component": "pkg/b", "owner_team": "sig/compute", "commits": 40},
        {"component": "pkg/c", "owner_team": "sig/storage", "commits": 25},
        {"component": "pkg/d", "owner_team": None, "commits": 500},
    ])
    churn = SnapshotOpenItems(store, lambda pid: {}).team_churn(ctx=None)
    assert churn == {"compute": 20, "storage": 5}


def test_no_churn_records_means_no_churn_term():
    assert SnapshotOpenItems(FakeStore([]), lambda pid: {}).team_churn(ctx=None) == {}


def test_churn_query_is_one_allow_listed_read():
    document = Q.component_churn(["pkg/virt-handler", "cmd/tool"])
    assert check_graphql({"query": document, "variables": {}, "operationName": "ComponentChurn"}) is None
    assert document.count("history(") == 2 and "mutation" not in document


@pytest.mark.parametrize("path", ['pkg/a") { x }', "pkg/../secret", "pkg/a b"])
def test_churn_query_refuses_unsafe_paths(path):
    with pytest.raises(ValueError):
        Q.component_churn([path])
