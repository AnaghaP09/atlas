"""Live CI job listing from a project's public job-artefact store.

The gateway allows the folder listing only for the job folders a project configures. The listing
connector checks each job's latest build with a conditional request and reports failing jobs that
ran recently; old jobs are left out.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from atlas.connectors.project import ProjectSources, SourceSet
from atlas.connectors.prow import JobListing, parse_listing
from atlas.contracts import EgressPurpose, RunMode
from atlas.gateway import Gateway, GatewayRefused, RequestContext, Response, policy_check

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
BASE = "https://storage.example/bucket"
SOURCES = SourceSet((ProjectSources.from_mapping("example", {
    "repository": "example/project",
    "ci": {"artifact_base": BASE, "listing_prefixes": ["logs/periodic-project-"]},
}),))
LIVE = RequestContext(purpose=EgressPurpose.LIVE_INGEST, run_mode=RunMode.LIVE)


def listing_xml(folders, next_marker=None):
    body = "".join(f"<CommonPrefixes><Prefix>{f}</Prefix></CommonPrefixes>" for f in folders)
    tail = f"<IsTruncated>true</IsTruncated><NextMarker>{next_marker}</NextMarker>" if next_marker else "<IsTruncated>false</IsTruncated>"
    return f"<ListBucketResult><Prefix>logs/periodic-project-</Prefix>{tail}{body}</ListBucketResult>"


class FakeStore:
    """Two listing pages and three jobs: one failing recently, one passing, one last run long ago."""

    def __init__(self):
        self.calls = []
        self.builds = {
            "logs/periodic-project-a/": ("101", {"result": "FAILURE", "timestamp": int((NOW - timedelta(hours=2)).timestamp())}),
            "logs/periodic-project-b/": ("202", {"result": "SUCCESS", "timestamp": int((NOW - timedelta(hours=3)).timestamp())}),
            "logs/periodic-project-old/": ("9", {"result": "FAILURE", "timestamp": int((NOW - timedelta(days=90)).timestamp())}),
        }

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, dict(headers)))
        path = url.split(BASE, 1)[1]
        if path.startswith("?"):
            if "marker=" in path:
                return Response(200, listing_xml(["logs/periodic-project-old/"]), {})
            return Response(200, listing_xml(["logs/periodic-project-a/", "logs/periodic-project-b/"], "logs/periodic-project-b/"), {})
        for folder, (build, finished) in self.builds.items():
            if path == f"/{folder}latest-build.txt":
                if headers.get("If-None-Match") == f'"{build}"':
                    return Response(304, None, {})
                return Response(200, build + "\n", {"ETag": f'"{build}"'})
            if path == f"/{folder}{build}/finished.json":
                return Response(200, finished, {})
        return Response(404, "not found", {})


@pytest.fixture
def listing():
    fake = FakeStore()
    gateway = Gateway(fake, clock=lambda: NOW, sources=SOURCES)
    return fake, JobListing(gateway, artifact_base=BASE, listing_prefixes=("logs/periodic-project-",), clock=lambda: NOW)


def test_parse_listing_returns_folders_and_next_marker():
    folders, marker = parse_listing(listing_xml(["logs/a/", "logs/b/"], "logs/b/"), "logs/")
    assert folders == ["logs/a/", "logs/b/"] and marker == "logs/b/"
    assert parse_listing(listing_xml(["logs/a/"]), "logs/")[1] is None


def test_failing_jobs_are_recent_failures_only(listing):
    fake, jobs = listing
    result = jobs.poll()
    assert result["jobs"] == 3 and result["checked"] == 3
    failing = jobs.failing()
    assert [f["job"] for f in failing] == ["periodic-project-a"]
    assert failing[0]["result"] == "FAILURE" and failing[0]["build_id"] == "101"
    cover = jobs.coverage()
    assert cover == {"jobs_listed": 3, "jobs_checked": 3, "listing_complete": True, "recent_jobs": 2}


def test_unchanged_job_costs_one_conditional_request(listing):
    fake, jobs = listing
    jobs.poll()
    before = len(fake.calls)
    second = jobs.poll()
    assert second["changed"] == []
    new_calls = fake.calls[before:]
    # The two recent jobs are revalidated with their validator; the old job is not asked again.
    assert len(new_calls) == 2 and all("If-None-Match" in h for _, _, h in new_calls)
    assert not any("periodic-project-old" in url for _, url, _ in new_calls)


def test_new_failure_is_reported_as_changed(listing):
    fake, jobs = listing
    jobs.poll()
    fake.builds["logs/periodic-project-b/"] = ("203", {"result": "FAILURE", "timestamp": int(NOW.timestamp())})
    assert jobs.poll()["changed"] == ["logs/periodic-project-b/"]
    assert {f["job"] for f in jobs.failing()} == {"periodic-project-a", "periodic-project-b"}


def test_running_build_keeps_the_previous_result(listing):
    fake, jobs = listing
    jobs.poll()
    fake.builds["logs/periodic-project-a/"] = ("102", None)  # started, no result yet

    def call(method, url, headers, body, inner=fake.__call__):
        if url.endswith("/102/finished.json"):
            return Response(404, "not found", {})
        return inner(method, url, headers, body)

    jobs.gateway._transport = call
    assert jobs.poll()["changed"] == []
    assert jobs.view("logs/periodic-project-a/")["build_id"] == "101"


@pytest.mark.parametrize("url, allowed", [
    (f"{BASE}?prefix=logs/periodic-project-&delimiter=/&max-keys=1000", True),
    (f"{BASE}?prefix=logs/periodic-project-&delimiter=/&marker=logs/periodic-project-b/", True),
    (f"{BASE}?prefix=logs/&delimiter=/", False),                       # wider than the configured folders
    (f"{BASE}?prefix=logs/periodic-project-", False),                  # no delimiter: would list every object
    (f"{BASE}?prefix=logs/periodic-project-&delimiter=/&versions=1", False),
    ("https://storage.example/other?prefix=logs/periodic-project-&delimiter=/", False),
    ("https://elsewhere.example/bucket?prefix=logs/periodic-project-&delimiter=/", False),
])
def test_gateway_allows_only_the_configured_folder_listing(url, allowed):
    reason = policy_check("ci", "GET", url, None, LIVE, SOURCES)
    assert (reason is None) is allowed


def test_listing_is_refused_without_configuration_and_for_writes():
    url = f"{BASE}?prefix=logs/periodic-project-&delimiter=/"
    assert policy_check("ci", "GET", url, None, LIVE, SourceSet()) is not None
    assert policy_check("ci", "POST", url, {}, LIVE, SOURCES) is not None
    replay = RequestContext(purpose=EgressPurpose.LIVE_INGEST, run_mode=RunMode.REPLAY)
    assert policy_check("ci", "GET", url, None, replay, SOURCES) is not None


def test_refused_listing_raises_through_the_gateway():
    gateway = Gateway(FakeStore(), clock=lambda: NOW, sources=SOURCES)
    jobs = JobListing(gateway, artifact_base=BASE, listing_prefixes=("logs/other-",), clock=lambda: NOW)
    with pytest.raises(GatewayRefused):
        jobs.list_jobs()


def test_result_file_served_as_plain_text_is_still_read(listing):
    import json as _json

    fake, jobs = listing
    inner = fake.__call__

    def as_text(method, url, headers, body):
        resp = inner(method, url, headers, body)
        if url.endswith("finished.json") and isinstance(resp.body, dict):
            return Response(resp.status, _json.dumps(resp.body), resp.headers)
        return resp

    jobs.gateway._transport = as_text
    jobs.poll()
    assert [f["job"] for f in jobs.failing()] == ["periodic-project-a"]
    assert jobs.is_recent("logs/periodic-project-a/") and not jobs.is_recent("logs/periodic-project-old/")
