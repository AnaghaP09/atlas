"""A small fake of the public GitHub and OSV APIs for Add project tests. Names are made up."""

from __future__ import annotations

import base64
import json
from typing import Any, Mapping
from urllib.parse import parse_qs, urlsplit

from atlas.gateway import Response

REPO = "example-org/widget"
SHA = "a" * 40
TAG = "v2.0.0"
CUTOFF = "2026-03-01T12:00:00Z"
BEFORE = "2026-02-01T00:00:00Z"
AFTER = "2026-04-01T00:00:00Z"


def _b64(text: str) -> dict[str, Any]:
    return {"encoding": "base64", "content": base64.b64encode(text.encode()).decode(), "sha": "b" * 40}


UV_LOCK = """
version = 1
[[package]]
name = "widget"
version = "2.0.0"
source = { editable = "." }
[[package]]
name = "oldlib"
version = "1.0.0"
[[package]]
name = "safe"
version = "3.0.0"
"""


def _issue(n: int, created: str, closed: str | None = None, labels=("bug",), updated: str | None = None) -> dict[str, Any]:
    return {"number": n, "title": f"Widget fails {n}", "body": f"Steps to reproduce {n}. Reported by alice-example",
            "created_at": created, "updated_at": updated or created, "closed_at": closed,
            "state": "closed" if closed else "open", "user": {"login": "alice-example"},
            "labels": [{"name": x} for x in labels], "html_url": f"https://github.com/{REPO}/issues/{n}"}


ISSUES = [
    _issue(1, "2026-01-05T00:00:00Z", updated="2026-01-06T00:00:00Z"),          # open at the tag, still open
    _issue(2, "2026-01-10T00:00:00Z", closed=AFTER, updated=AFTER),             # open at the tag, closed later
    _issue(3, "2026-03-10T00:00:00Z"),                                          # created after the tag
    _issue(4, "2026-01-02T00:00:00Z", closed=BEFORE, updated=BEFORE),           # closed before the tag
]

TIMELINES = {
    1: [{"event": "labeled", "created_at": "2026-01-05T00:00:01Z", "label": {"name": "bug"}},
        {"event": "commented", "created_at": "2026-01-06T00:00:00Z", "updated_at": "2026-01-06T00:00:00Z",
         "body": "Same here, cc @bob-example", "user": {"login": "carol-example"}, "id": 11},
        {"event": "commented", "created_at": AFTER, "updated_at": AFTER, "body": "Fixed in the next release",
         "user": {"login": "dave-example"}, "id": 12},
        {"event": "labeled", "created_at": AFTER, "label": {"name": "wontfix"}}],
    2: [{"event": "labeled", "created_at": "2026-01-10T00:00:01Z", "label": {"name": "bug"}},
        {"event": "closed", "created_at": AFTER}],
}


class FakeGitHub:
    """Answers the reads Add project makes. Records every request."""

    def __init__(self, *, labels: list[dict[str, Any]] | None = None, issues: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.labels = labels if labels is not None else [
            {"name": "bug", "description": "Something is broken"}, {"name": "kind/flake"},
            {"name": "not a bug"}, {"name": "priority: critical"}, {"name": "docs"}]
        self.issues = issues if issues is not None else ISSUES

    def __call__(self, method: str, url: str, headers: Mapping[str, str], body: Any) -> Response:
        parts = urlsplit(url)
        path, q = parts.path, parse_qs(parts.query)
        self.calls.append((method, path))
        base = f"/repos/{REPO}"
        if parts.hostname == "api.osv.dev":
            return self.osv(method, path, body)
        routes: dict[str, Any] = {
            base: {"full_name": REPO, "default_branch": "main", "size": 900, "open_issues_count": 2,
                   "description": "A made-up widget library", "html_url": f"https://github.com/{REPO}"},
            f"{base}/languages": {"Python": 9000, "JavaScript": 1000},
            f"{base}/labels": self.labels,
            f"{base}/releases": [{"tag_name": TAG, "published_at": CUTOFF, "name": "2.0"},
                                 {"tag_name": "v2.1.0-rc.1", "prerelease": True, "published_at": AFTER}],
            f"{base}/tags": [{"name": TAG, "commit": {"sha": SHA}}, {"name": "v1.0.0", "commit": {"sha": "c" * 40}}],
            f"{base}/contents": [{"name": "uv.lock", "type": "file"}, {"name": ".github", "type": "dir"},
                                 {"name": "src", "type": "dir"}],
            f"{base}/contents/.github": [{"name": "workflows", "type": "dir"}, {"name": "CODEOWNERS", "type": "file"}],
            f"{base}/actions/workflows": {"total_count": 2, "workflows": [{"name": "Tests"}, {"name": "Lint"}]},
            f"{base}/git/ref/tags/{TAG}": {"object": {"sha": SHA, "type": "commit"}},
            f"{base}/commits/{SHA}": {"sha": SHA, "commit": {"committer": {"date": CUTOFF}}},
            f"{base}/commits": [{"sha": SHA, "commit": {"message": "Release 2.0.0", "committer": {"date": CUTOFF}},
                                 "author": {"login": "erin-example"}, "parents": []}],
            f"{base}/git/trees/{SHA}": {"truncated": False, "tree": [
                {"path": "src/widget/__init__.py", "type": "blob", "size": 10},
                {"path": "src/widget/core.py", "type": "blob", "size": 10},
                {"path": "src/widget/web/__init__.py", "type": "blob", "size": 10},
                {"path": "tests/test_core.py", "type": "blob", "size": 10},
                {"path": "uv.lock", "type": "blob", "size": 10},
                {"path": ".github/CODEOWNERS", "type": "blob", "size": 10}]},
            f"{base}/contents/uv.lock": _b64(UV_LOCK),
            f"{base}/contents/.github/CODEOWNERS": _b64("/src/widget/web/ @example-org/web\n"),
            f"{base}/releases/latest": {"tag_name": TAG, "published_at": CUTOFF},
        }
        if method != "GET":
            return Response(404, {})
        if path == f"{base}/issues":
            items = list(self.issues)
            if q.get("state") == ["open"]:
                items = [i for i in items if not i["closed_at"]]
            if q.get("labels"):
                items = [i for i in items if any(lab["name"] == q["labels"][0] for lab in i["labels"])]
            return Response(200, items, {"x-ratelimit-remaining": "50"})
        if path.startswith(f"{base}/issues/") and path.endswith("/timeline"):
            n = int(path.split("/")[-2])
            return Response(200, TIMELINES.get(n, []))
        if path in routes:
            return Response(200, routes[path], {"etag": f'"{hash(path)}"'})
        return Response(404, {"message": "Not Found"})

    @staticmethod
    def osv(method: str, path: str, body: Any) -> Response:
        if method == "POST" and path == "/v1/querybatch":
            results = []
            for q in body["queries"]:
                name = q["package"]["name"]
                assert q["package"]["ecosystem"] == "PyPI"
                results.append({"vulns": [{"id": "OSV-OLD-1"}, {"id": "OSV-NEW-2"}]} if name == "oldlib" else {})
            return Response(200, {"results": results})
        if method == "GET" and path == "/v1/vulns/OSV-OLD-1":
            return Response(200, {"id": "OSV-OLD-1", "published": "2025-01-01T00:00:00Z", "summary": "old flaw",
                                  "affected": [{"package": {"name": "oldlib"}, "ranges": [{"events": [{"fixed": "1.1.0"}]}]}]})
        if method == "GET" and path == "/v1/vulns/OSV-NEW-2":
            return Response(200, {"id": "OSV-NEW-2", "published": AFTER, "summary": "flaw published after the tag"})
        return Response(404, {})


def setting(**kw: Any) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(**kw)


def dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True)
