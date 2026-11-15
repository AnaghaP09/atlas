"""The GitHub publication adapter and the publication policy of the gateway, against a fake remote."""

import base64
import re
from datetime import datetime, timedelta, timezone

import pytest

from atlas.contracts import EgressPurpose, Mode
from atlas.execution.github_publication import GitHubPublicationAPI, PublicationUnavailable
from atlas.gateway import Gateway, GatewayRefused, PublishGrant, RequestContext, Response, SourceSet, policy_check

REPO = "example/target"
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
GRANT = PublishGrant(REPO, "atlas/act-1", "main", NOW + timedelta(hours=1))


class FakeGitHub:
    """A small stateful stand-in for the GitHub REST routes that publication uses."""

    def __init__(self) -> None:
        self.refs = {"main": "a" * 40}
        self.commits = {"a" * 40: {"tree": "t" * 40, "parents": [], "message": "base"}}
        self.blobs: dict[str, str] = {}
        self.trees: dict[str, dict] = {}
        self.pulls: list[dict] = []
        self.requests: list[tuple[str, str]] = []
        self.auth: list[str] = []

    @staticmethod
    def _sha(prefix: str, n: int) -> str:
        return (prefix + f"{n:x}").ljust(40, "0")

    def __call__(self, method, url, headers, body):
        path = url.split("https://api.github.com", 1)[1]
        self.requests.append((method, path))
        self.auth.append(headers.get("Authorization", ""))
        base = f"/repos/{REPO}"
        if method == "GET" and path == base:
            return Response(200, {"node_id": "R_1", "full_name": REPO})
        m = re.match(rf"^{base}/git/ref/heads/(.+)$", path)
        if method == "GET" and m:
            sha = self.refs.get(m.group(1))
            return Response(200, {"object": {"sha": sha}}) if sha else Response(404, {})
        m = re.match(rf"^{base}/git/commits/([0-9a-f]+)$", path)
        if method == "GET" and m:
            return Response(200, {"tree": {"sha": self.commits[m.group(1)]["tree"]}})
        if method == "POST" and path == f"{base}/git/blobs":
            sha = self._sha("b", len(self.blobs) + 1)
            self.blobs[sha] = base64.b64decode(body["content"]).decode("utf-8")
            return Response(201, {"sha": sha})
        if method == "POST" and path == f"{base}/git/trees":
            sha = self._sha("d", len(self.trees) + 1)
            self.trees[sha] = body
            return Response(201, {"sha": sha})
        if method == "POST" and path == f"{base}/git/commits":
            sha = self._sha("c", len(self.commits) + 1)
            self.commits[sha] = body
            return Response(201, {"sha": sha})
        if method == "POST" and path == f"{base}/git/refs":
            name = body["ref"].removeprefix("refs/heads/")
            if name in self.refs:
                return Response(422, {"message": "Reference already exists"})
            self.refs[name] = body["sha"]
            return Response(201, {"ref": body["ref"]})
        if method == "GET" and path.startswith(f"{base}/pulls?"):
            return Response(200, list(self.pulls))
        if method == "POST" and path == f"{base}/pulls":
            pr = {"number": len(self.pulls) + 1, "head": {"ref": body["head"]}, "base": {"ref": body["base"]},
                  "title": body["title"], "body": body["body"], "draft": body["draft"], "state": "open"}
            self.pulls.append(pr)
            return Response(201, pr)
        m = re.match(rf"^{base}/pulls/(\d+)$", path)
        if m and method == "GET":
            return Response(200, self.pulls[int(m.group(1)) - 1])
        if m and method == "PATCH":
            self.pulls[int(m.group(1)) - 1]["body"] = body["body"]
            return Response(200, self.pulls[int(m.group(1)) - 1])
        return Response(404, {})


def make(grant=GRANT, mode=Mode.CONNECTED, sources=None, now=NOW):
    remote = FakeGitHub()
    violations = []
    gateway = Gateway(remote, mode=lambda: mode, on_violation=lambda kind, detail: violations.append(kind),
                      tokens={"publish": lambda: "test-token-value", "github": lambda: "other"},
                      clock=lambda: now, sources=sources or SourceSet())
    api = GitHubPublicationAPI(gateway, lambda repo: grant if grant and repo == grant.destination_repo else None)
    return api, remote, gateway, violations


def test_push_draft_pull_and_description_reach_the_remote_once():
    api, remote, gateway, violations = make()
    assert api.repo_identity(REPO).node_id == "R_1"
    assert api.branch_head(REPO, "atlas/act-1") is None
    files = {"netaddr/netaddr.go": "package netaddr\n"}
    commit = api.push_branch(REPO, "atlas/act-1", "local-tree-id", files, "Fix it")
    assert remote.refs["atlas/act-1"] == commit
    assert remote.commits[commit]["parents"] == ["a" * 40]
    assert list(remote.blobs.values()) == ["package netaddr\n"]
    pr = api.create_draft_pull(REPO, "atlas/act-1", "main", "Draft: fix", "")
    assert pr.draft and pr.number == 1
    api.replace_pull_body(REPO, pr.number, "What changed")
    assert api.get_pull(REPO, 1).body == "What changed"
    assert [p.number for p in api.find_pulls(REPO, "atlas/act-1")] == [1]
    assert violations == [] and gateway.refused() == []
    assert set(remote.auth) == {"Bearer test-token-value"}  # the publish token, never the read token


def test_without_a_grant_nothing_is_sent():
    api, remote, _, _ = make(grant=None)
    with pytest.raises(PublicationUnavailable):
        api.repo_identity(REPO)
    assert remote.requests == []


def ctx(grant=GRANT, **kw):
    return RequestContext(purpose=EgressPurpose.PUBLICATION, publish=grant, **kw)


REFUSED = [
    ("POST", "/repos/example/other/git/refs", {"ref": "refs/heads/atlas/act-1", "sha": "c" * 40}),
    ("POST", f"/repos/{REPO}/git/refs", {"ref": "refs/heads/main", "sha": "c" * 40}),
    ("PATCH", f"/repos/{REPO}/git/refs/heads/main", {"sha": "c" * 40, "force": True}),
    ("POST", f"/repos/{REPO}/pulls", {"head": "atlas/act-1", "base": "main", "draft": False, "title": "x", "body": ""}),
    ("POST", f"/repos/{REPO}/pulls", {"head": "other", "base": "main", "draft": True, "title": "x", "body": ""}),
    ("PATCH", f"/repos/{REPO}/pulls/1", {"body": "x", "state": "closed"}),
    ("PUT", f"/repos/{REPO}/pulls/1/merge", {}),
    ("DELETE", f"/repos/{REPO}/git/refs/heads/atlas/act-1", None),
    ("POST", f"/repos/{REPO}/issues/1/comments", {"body": "x"}),
    ("GET", f"/repos/{REPO}/git/ref/heads/release", None),
]


@pytest.mark.parametrize("method,path,body", REFUSED)
def test_requests_outside_the_grant_are_refused(method, path, body):
    url = "https://api.github.com" + path
    assert policy_check("publication", method, url, body, ctx(), SourceSet(), NOW) is not None


def test_expired_approval_frozen_run_and_missing_grant_are_refused():
    url = f"https://api.github.com/repos/{REPO}"
    expired = PublishGrant(REPO, "atlas/act-1", "main", NOW - timedelta(seconds=1))
    assert "expired" in policy_check("publication", "GET", url, None, ctx(expired), SourceSet(), NOW)
    assert policy_check("publication", "GET", url, None, ctx(frozen_evaluation=True), SourceSet(), NOW) is not None
    assert policy_check("publication", "GET", url, None, ctx(None), SourceSet(), NOW) is not None
    assert policy_check("issues", "GET", url, None, ctx(), SourceSet(), NOW) is not None


def test_a_refused_write_is_counted_and_never_sent():
    _, remote, gateway, violations = make()
    with pytest.raises(GatewayRefused):
        gateway.request("publication", "PUT", f"https://api.github.com/repos/{REPO}/pulls/1/merge", ctx(), {})
    assert remote.requests == [] and violations == ["allow_list_violation"]


def test_offline_mode_refuses_publication():
    api, remote, _, violations = make(mode=Mode.OFFLINE)
    with pytest.raises(GatewayRefused):
        api.repo_identity(REPO)
    assert remote.requests == [] and violations == ["offline_egress_attempt"]


def test_a_write_is_sent_once_even_when_the_remote_fails():
    api, remote, gateway, _ = make()
    calls = []

    def failing(method, url, headers, body):
        calls.append(method)
        if method == "POST":
            raise ConnectionError("lost")
        return remote(method, url, headers, body)

    gateway._transport = failing
    with pytest.raises(ConnectionError):
        api.create_draft_pull(REPO, "atlas/act-1", "main", "t", "")
    assert calls.count("POST") == 1
