"""Live dashboard polling without a GitHub token.

GitHub's detailed issue history (GraphQL) needs credentials. Without them the live store keeps the
REST view of each issue, marked as having no edit history, so the dashboard still shows real data.
Such records never count as complete history. Rate-limit headers are recorded so polling can wait
for the reset.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from atlas.connectors.github import GitHubError, GitHubReader
from atlas.connectors.issues import IssuesConnector
from atlas.connectors.normalize import issue_from_rest
from atlas.connectors.project import ProjectSources, SourceSet
from atlas.contracts import EgressPurpose, RunMode
from atlas.gateway import Gateway, RequestContext, Response
from atlas.snapshots import SnapshotRegistry
from atlas.store import LIVE_SNAPSHOT_ID
from tests.a.helpers import ctx

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
EXAMPLE = SourceSet((ProjectSources.from_mapping("example", {"repository": "kubevirt/kubevirt"}),))
LIVE_CTX = RequestContext(purpose=EgressPurpose.LIVE_INGEST, run_mode=RunMode.LIVE)
RESET = int(datetime(2026, 11, 14, 12, 30, tzinfo=timezone.utc).timestamp())

REST_ISSUE = {
    "number": 101, "title": "Flaky migration test", "body": "Fails one run in five.", "state": "open",
    "labels": [{"name": "kind/flake"}, {"name": "sig/compute"}], "created_at": "2026-10-01T08:00:00Z",
    "updated_at": "2026-11-13T09:00:00Z", "comments": 3, "user": {"login": "someone"},
    "html_url": "https://github.com/kubevirt/kubevirt/issues/101",
}


class NoTokenGitHub:
    """REST answers normally; GraphQL refuses as GitHub does for unauthenticated callers."""

    def __init__(self, graphql_status: int = 401, remaining: int = 50) -> None:
        self.graphql_status = graphql_status
        self.remaining = remaining

    def __call__(self, method, url, headers, body):
        h = {"X-RateLimit-Remaining": str(self.remaining), "X-RateLimit-Reset": str(RESET)}
        if url.endswith("/graphql"):
            return Response(self.graphql_status, {"message": "requires authentication"}, h)
        return Response(200, REST_ISSUE, dict(h, ETag='"v1"'))


@pytest.fixture
def make(tmp_path):
    registries = []

    def build(fake):
        gateway = Gateway(fake, clock=lambda: NOW, sources=EXAMPLE)
        reader = GitHubReader(gateway, LIVE_CTX, owner="kubevirt", name="kubevirt")
        registry = SnapshotRegistry(tmp_path / f"data{len(registries)}")
        registries.append(registry)
        store = registry.store()
        return reader, store, IssuesConnector(store, reader, clock=lambda: NOW)

    yield build
    for r in registries:
        r.close()


def test_rest_view_is_live_only_and_never_complete_history() -> None:
    asof = issue_from_rest(REST_ISSUE, NOW)
    assert asof is not None and asof.history_complete is False
    assert asof.view["edit_history"] == "unknown"
    assert asof.view["labels"] == ["kind/flake", "sig/compute"] and asof.view["state"] == "OPEN"
    assert issue_from_rest(dict(REST_ISSUE, pull_request={}), NOW) is None


def test_fetch_live_falls_back_to_rest_when_history_needs_credentials(make) -> None:
    reader, store, conn = make(NoTokenGitHub())
    live = ctx(LIVE_SNAPSHOT_ID, mode=RunMode.LIVE)
    recs = conn.fetch_live({"number": 101}, live)
    assert len(recs) == 1 and recs[0].confirmed_through == NOW
    current = store.current_live("issues", "issue/101")
    assert current is not None and current.record_id == recs[0].record_id
    assert reader.rate_remaining == 50 and reader.rate_reset == RESET


def test_other_graphql_errors_still_fail_the_poll(make) -> None:
    _, _, conn = make(NoTokenGitHub(graphql_status=422))
    with pytest.raises(GitHubError) as err:
        conn.fetch_live({"number": 101}, ctx(LIVE_SNAPSHOT_ID, mode=RunMode.LIVE))
    assert err.value.status == 422
