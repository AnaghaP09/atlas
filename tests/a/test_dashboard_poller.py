"""The live dashboard poller: real open counts, CI job results, a periodic dependency scan and history.

Uses a fake transport, so no network is touched.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest

import atlas.wiring  # noqa: F401  (import order: the API package loads the feed)
from atlas.connectors.dependency_scan import ROOT_COMPONENT
from atlas.connectors.project import ProjectSources, SourceSet
from atlas.contracts import SecurityState
from atlas.data_wiring import SCAN_EVERY, ProjectConfig, ProjectPoller
from atlas.feed import ProjectFeed
from atlas.gateway import Gateway, Response
from atlas.security_gate import evaluate, parse_security_records
from atlas.snapshots import SnapshotRegistry

BASE = "https://storage.example/bucket"
GO_MOD = "module example.org/project\n\nrequire (\n\texample.org/lib v1.2.0\n\texample.org/other v0.4.1\n)\n"


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


class Hub:
    def __init__(self):
        self.events = []

    def publish(self, stream, wire, scopes, project_id=None):
        self.events.append(wire["event"])


class FakeSources:
    """GitHub REST, the job store and OSV for one small project."""

    def __init__(self, clock, issues=45, vulnerable=False):
        self.clock = clock
        self.issues = issues
        self.vulnerable = vulnerable
        self.calls = []

    def _issue(self, n):
        return {"number": n, "title": f"Flaky test {n}", "body": "x", "state": "open",
                "labels": [{"name": "kind/flake"}], "created_at": "2026-10-01T08:00:00Z",
                "updated_at": f"2026-11-1{n % 5}T09:00:00Z", "comments": 0, "user": {"login": "someone"}}

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url))
        if "api.osv.dev" in url:
            if url.endswith("/v1/querybatch"):
                first = {"vulns": [{"id": "GHSA-aaaa"}]} if self.vulnerable else {}
                return Response(200, {"results": [first, {}]}, {})
            return Response(200, {"id": "GHSA-aaaa", "published": "2026-05-01T00:00:00Z",
                                  "database_specific": {"severity": "HIGH"},
                                  "affected": [{"package": {"name": "example.org/lib"},
                                                "ranges": [{"events": [{"fixed": "1.2.5"}]}]}]}, {})
        if url.startswith(BASE):
            path = url[len(BASE):]
            if path.startswith("?"):
                return Response(200, "<ListBucketResult><IsTruncated>false</IsTruncated>"
                                     "<CommonPrefixes><Prefix>logs/periodic-project-a/</Prefix></CommonPrefixes></ListBucketResult>", {})
            if path.endswith("latest-build.txt"):
                return Response(200, "77", {"ETag": '"77"'})
            return Response(200, {"result": "FAILURE", "timestamp": int((self.clock() - timedelta(hours=1)).timestamp())}, {})
        if "/contents/go.mod" in url:
            return Response(200, {"encoding": "base64", "content": base64.b64encode(GO_MOD.encode()).decode(), "sha": "blob1"}, {})
        if "/releases/latest" in url:
            return Response(200, {"tag_name": "v1.0.0", "published_at": "2026-11-01T00:00:00Z", "name": "v1.0.0"}, {})
        if "/issues" in url:
            query = {k: v for k, v in (pair.split("=", 1) for pair in url.split("?", 1)[1].split("&"))}
            page, per_page = int(query.get("page", 1)), int(query["per_page"])
            start = (page - 1) * per_page
            return Response(200, [self._issue(n) for n in range(start + 1, min(self.issues, start + per_page) + 1)], {})
        return Response(404, {}, {})


@pytest.fixture
def make(tmp_path):
    clock = Clock()

    def build(fake, authenticated=False):
        sources = ProjectSources.from_mapping("example", {
            "repository": "example/project", "ci": {"artifact_base": BASE, "listing_prefixes": ["logs/periodic-project-"]}})
        config = ProjectConfig(sources=sources, ci="prow", labels={"flaky": "kind/flake"}, security_inventory="go.mod")
        gateway = Gateway(fake, clock=clock, sources=SourceSet((sources,)))
        registry = SnapshotRegistry(tmp_path / "data", sources=SourceSet((sources,)))
        feed = ProjectFeed(Hub(), clock=clock)
        poller = ProjectPoller("example", config, gateway, registry, feed, clock=clock, authenticated=authenticated,
                               history_path=tmp_path / "data" / "local" / "stores" / "example" / "dashboard-history.json")
        return poller, feed, registry

    return clock, build


def panel(feed, name):
    return next(p for p in feed.dashboard("example")["panels"] if p["panel_id"] == name)


def test_panel_shows_newest_rows_and_the_real_total(make):
    clock, build = make
    poller, feed, _ = build(FakeSources(clock, issues=45))
    result = poller.poll_once()
    assert result["polled"] is True and result["open_issues"] == 30  # one page without a token
    issues = panel(feed, "open_issues")
    assert len(issues["items"]) == 20 and issues["total"] == 30


def test_with_a_token_the_first_poll_walks_every_page(make):
    clock, build = make
    fake = FakeSources(clock, issues=245)
    poller, feed, _ = build(fake, authenticated=True)
    poller.max_enrich = 0  # history enrichment needs GraphQL, which this fake does not serve
    assert poller.poll_once()["open_issues"] == 245
    assert panel(feed, "open_issues")["total"] == 245
    pages = [u for _, u in fake.calls if "/issues" in u]
    assert len(pages) == 3


def test_failing_ci_job_is_listed_and_announced(make):
    clock, build = make
    poller, feed, _ = build(FakeSources(clock))
    poller.poll_once()
    ci = panel(feed, "failing_ci")
    assert ci["total"] == 1
    assert "1 of 1 periodic jobs" in ci["items"][0]["note"]
    assert ci["items"][1]["job"] == "periodic-project-a" and ci["items"][1]["result"] == "FAILURE"
    assert any(a["kind"] == "ci" and "periodic-project-a" in a["title"] for a in feed.activity("example"))


def test_clean_dependency_scan_is_stored_and_lets_the_gate_pass(make):
    clock, build = make
    poller, feed, registry = build(FakeSources(clock))
    poller.poll_once()
    security = panel(feed, "security")
    assert security["total"] == 0 and "2 modules scanned" in security["items"][0]["note"]
    db = registry.live("example")
    records = db.search({"source": "security", "limit": 100})
    assert len(records) == 1
    scans, findings = parse_security_records(records, lambda r: json.dumps(db.content(r.record_id)))
    assert evaluate([ROOT_COMPONENT], scans, findings, [], "p", clock()).state is SecurityState.PASSED


def test_blocking_finding_is_shown_and_restricted_to_security(make):
    clock, build = make
    poller, feed, registry = build(FakeSources(clock, vulnerable=True))
    poller.poll_once()
    rows = panel(feed, "security")["items"]
    finding = rows[1]
    assert finding["module"] == "example.org/lib" and finding["blocking"] is True
    assert finding["required_scopes"] == ["restricted:security"] and "required_scopes" not in rows[0]


def test_scan_runs_on_a_six_hour_budget(make):
    clock, build = make
    fake = FakeSources(clock)
    poller, _, _ = build(fake)
    poller.poll_once()
    scans = lambda: sum(1 for _, u in fake.calls if u.endswith("/v1/querybatch"))  # noqa: E731
    assert scans() == 1
    clock.now += timedelta(hours=5)
    poller.poll_once()
    assert scans() == 1
    clock.now += SCAN_EVERY
    poller.poll_once()
    assert scans() == 2


def test_history_survives_a_restart(make):
    clock, build = make
    poller, feed, _ = build(FakeSources(clock))
    poller.poll_once()
    clock.now += timedelta(minutes=1)
    poller.poll_once()
    assert len(feed.dashboard("example")["history"]) == 2
    again, feed2, _ = build(FakeSources(clock))
    history = feed2.dashboard("example")["history"]
    assert len(history) == 2 and history[-1]["open_issues"] == 30 and history[-1]["failing_ci"] == 1
    # After a restart the stored scan is reused instead of scanning again within the budget.
    fake = FakeSources(clock)
    third, feed3, _ = build(fake)
    third.poll_once()
    assert not any(u.endswith("/v1/querybatch") for _, u in fake.calls)
    assert "2 modules scanned" in panel(feed3, "security")["items"][0]["note"]
