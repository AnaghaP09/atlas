"""A live record stays fresh while successful polls keep confirming it, and only then."""

from datetime import datetime, timedelta, timezone

import pytest

from atlas.connectors.github import GitHubError, GitHubReader
from atlas.connectors.issues import IssuesConnector
from atlas.contracts import EgressPurpose, RunMode
from atlas.freshness import FreshnessStatus, evaluate
from atlas.gateway import Gateway, GatewayRefused, RequestContext, Response, SourceUnavailable
from atlas.snapshots import SnapshotRegistry
from atlas.store import LIVE_SNAPSHOT_ID, EvidenceDB, ReadOnlyStore, WriterRejected
from tests.a.helpers import ctx, rec

T0 = datetime(2026, 11, 14, 9, 0, tzinfo=timezone.utc)
LIVE_CTX = RequestContext(purpose=EgressPurpose.LIVE_INGEST, run_mode=RunMode.LIVE)

ISSUE_GRAPHQL = {
    "data": {"repository": {"issue": {
        "number": 42, "title": "Flaky test", "body": "unchanged body", "createdAt": "2026-10-01T00:00:00Z",
        "closedAt": None, "state": "OPEN", "stateReason": None, "url": "https://github.com/kubevirt/kubevirt/issues/42",
        "author": {"login": "someone"}, "labels": {"nodes": []},
        "userContentEdits": {"totalCount": 0, "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []},
        "timelineItems": {"totalCount": 0, "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []},
        "comments": {"totalCount": 0, "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []},
    }}}
}


class GitHubFake:
    """Serves the issue; answers 304 when the client presents the current ETag."""

    def __init__(self):
        self.etag = '"v1"'
        self.fail = False
        self.conditional_seen = []
        self.graphql_calls = 0

    def __call__(self, method, url, headers, body):
        if self.fail:
            raise ConnectionError("network down")
        if url.endswith("/graphql"):
            self.graphql_calls += 1
            return Response(200, ISSUE_GRAPHQL)
        self.conditional_seen.append(headers.get("If-None-Match"))
        if headers.get("If-None-Match") == self.etag:
            return Response(304, None, {"ETag": self.etag})
        return Response(200, {"number": 42, "title": "Flaky test"}, {"ETag": self.etag})


@pytest.fixture
def env(tmp_path):
    clock = {"now": T0}
    fake = GitHubFake()
    gateway = Gateway(fake, clock=lambda: clock["now"])
    reader = GitHubReader(gateway, LIVE_CTX)
    registry = SnapshotRegistry(tmp_path / "data")
    store = registry.store()
    connector = IssuesConnector(store, reader, clock=lambda: clock["now"])
    live = ctx(LIVE_SNAPSHOT_ID, mode=RunMode.LIVE)
    yield clock, fake, connector, store, live, registry
    registry.close()


def poll(connector, live):
    return connector.fetch(  # refresh=True selects the live path
        {"number": 42, "refresh": True}, live
    )[0]


def test_repeated_unchanged_polls_stay_fresh_across_the_boundary(env):
    clock, fake, connector, store, live, _ = env
    first = poll(connector, live)
    assert first.confirmed_through == T0
    full_fetch_calls = fake.graphql_calls
    for minutes in (30, 59, 90, 150):  # well past the one-hour limit from the first fetch
        clock["now"] = T0 + timedelta(minutes=minutes)
        again = poll(connector, live)
        assert again.record_id == first.record_id
        assert again.confirmed_through == clock["now"]
        stored = store.get(live, "issues", "issue/42")
        assert evaluate("issues", stored.confirmed_through, clock["now"]).status is FreshnessStatus.FRESH
    assert '"v1"' in fake.conditional_seen  # the 304 revalidation path was used
    assert fake.graphql_calls == full_fetch_calls  # unchanged polls did not refetch the full issue


def test_failed_polls_do_not_extend_freshness(env):
    clock, fake, connector, store, live, _ = env
    poll(connector, live)
    fake.fail = True
    clock["now"] = T0 + timedelta(minutes=50)
    with pytest.raises((ConnectionError, SourceUnavailable)):
        poll(connector, live)
    clock["now"] = T0 + timedelta(minutes=61)
    stored = store.get(live, "issues", "issue/42")
    assert stored.confirmed_through == T0
    assert evaluate("issues", stored.confirmed_through, clock["now"]).status is FreshnessStatus.STALE


def test_changed_content_is_a_new_version_confirmed_by_the_fetch(env):
    clock, fake, connector, store, live, _ = env
    first = poll(connector, live)
    fake.etag = '"v2"'
    ISSUE_GRAPHQL["data"]["repository"]["issue"]["body"] = "edited body"
    try:
        clock["now"] = T0 + timedelta(minutes=5)
        second = poll(connector, live)
    finally:
        ISSUE_GRAPHQL["data"]["repository"]["issue"]["body"] = "unchanged body"
    assert second.record_id != first.record_id and second.confirmed_through == clock["now"]


def test_bodyless_304_returns_cached_body_and_304_without_cache_is_an_error(env):
    _, fake, connector, _, _, _ = env
    reader = connector.reader
    body, unchanged = reader.rest_conditional("/issues/42")
    assert unchanged is False and body["number"] == 42
    body2, unchanged2 = reader.rest_conditional("/issues/42")
    assert unchanged2 is True and body2 == body
    fresh_reader = GitHubReader(Gateway(lambda m, u, h, b: Response(304, None)), LIVE_CTX)
    with pytest.raises(GitHubError):
        fresh_reader.rest_conditional("/issues/42")


def test_gateway_allows_only_conditional_headers_on_get():
    g = Gateway(lambda m, u, h, b: Response(200, {}))
    url = "https://api.github.com/repos/kubevirt/kubevirt/issues/42"
    g.request("issues", "GET", url, LIVE_CTX, conditional={"If-None-Match": '"x"', "If-Modified-Since": "Fri, 13 Nov 2026 00:00:00 GMT"})
    with pytest.raises(GatewayRefused):
        g.request("issues", "GET", url, LIVE_CTX, conditional={"Authorization": "Bearer x"})
    with pytest.raises(GatewayRefused):
        g.request("issues", "POST", "https://api.github.com/graphql", LIVE_CTX, {"query": "query RateLimit { rateLimit { remaining } }"}, conditional={"If-None-Match": '"x"'})


def test_replayed_delivery_does_not_refresh(tmp_path):
    db = EvidenceDB(tmp_path / "live.sqlite", live=True)
    r = rec(confirmed_through=T0)
    db.put(r, "connector.issues", event_key="evt-1")
    # The same event delivered again an hour later is a no-op and confirms nothing.
    db.put(r, "connector.issues", event_key="evt-1")
    db.put(rec(confirmed_through=T0 + timedelta(hours=2)), "connector.issues")
    assert db.get("issues", "issue/1").confirmed_through == T0
    db.close()


def test_confirmations_only_in_the_live_store_and_by_the_authorised_writer(tmp_path):
    reg = SnapshotRegistry(tmp_path / "data")
    store = reg.store()
    store.put(rec(confirmed_through=T0), "connector.issues")
    info = reg.freeze(frozen_at=T0)
    frozen = reg.open(ctx(info.snapshot_id))
    with pytest.raises(ReadOnlyStore):
        frozen.reconfirm("issues:issue/1:v1", "connector.issues", T0 + timedelta(hours=1))
    plain = EvidenceDB(tmp_path / "not-live.sqlite")
    plain.put(rec(), "connector.issues")
    with pytest.raises(ReadOnlyStore):
        plain.reconfirm("issues:issue/1:v1", "connector.issues", T0)
    with pytest.raises(WriterRejected):
        store.reconfirm("issues:issue/1:v1", "connector.git", T0 + timedelta(hours=1))
    # Never moves backwards.
    store.reconfirm("issues:issue/1:v1", "connector.issues", T0 + timedelta(hours=2))
    store.reconfirm("issues:issue/1:v1", "connector.issues", T0 + timedelta(hours=1))
    live = ctx(LIVE_SNAPSHOT_ID, mode=RunMode.LIVE)
    assert store.get(live, "issues", "issue/1").confirmed_through == T0 + timedelta(hours=2)
    # The frozen snapshot keeps the confirmation it had when frozen.
    assert frozen.get("issues", "issue/1").confirmed_through == T0
    plain.close()
    reg.close()


def test_superseded_version_cannot_be_reconfirmed(tmp_path):
    db = EvidenceDB(tmp_path / "live.sqlite", live=True)
    db.put(rec(version="r1", revision=1), "connector.issues")
    db.put(rec(version="r2", revision=2), "connector.issues")
    with pytest.raises(ValueError):
        db.reconfirm("issues:issue/1:r1", "connector.issues", T0)
    db.close()
