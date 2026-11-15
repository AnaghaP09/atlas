"""Reading the target's identity and base tree through the gateway's publication policy."""

from __future__ import annotations

import re
from datetime import timedelta

import pytest

from atlas.contracts import EgressPurpose, Mode
from atlas.execution.github_publication import GitHubPublicationAPI, PublicationUnavailable
from atlas.gateway import Gateway, PublishGrant, RequestContext, Response, SourceSet, policy_check
from tests.c.test_github_publication import GRANT, NOW, REPO, FakeGitHub

TREE = "e" * 40
BASE = "a" * 40
API = "https://api.github.com"


class Remote(FakeGitHub):
    def __init__(self) -> None:
        super().__init__()
        self.commits[BASE]["tree"] = TREE
        self.files = {"go.mod": "1" * 40, "netaddr/netaddr.go": "2" * 40}
        self.truncated = False

    def __call__(self, method, url, headers, body):
        path = url.split(API, 1)[1]
        if method == "GET" and re.match(rf"^/repos/{REPO}/git/trees/{TREE}\?recursive=1$", path):
            self.requests.append((method, path))
            entries = [{"path": p, "type": "blob", "sha": s} for p, s in self.files.items()]
            entries.append({"path": "netaddr", "type": "tree", "sha": "3" * 40})
            return Response(200, {"tree": entries, "truncated": self.truncated})
        return super().__call__(method, url, headers, body)


def _api(remote, grant=GRANT):
    gateway = Gateway(remote, mode=lambda: Mode.CONNECTED, tokens={"publish": lambda: "token"}, clock=lambda: NOW,
                      sources=SourceSet())
    return GitHubPublicationAPI(gateway, lambda full_name: grant if full_name == REPO else None)


def test_identity_is_read_before_any_approval_exists():
    remote = Remote()
    identity = _api(remote, grant=None).identify(REPO, "main", NOW)
    assert identity.node_id == "R_1" and identity.full_name == REPO
    assert remote.requests == [("GET", f"/repos/{REPO}")]


def test_an_identity_read_can_do_nothing_else():
    probe = RequestContext(purpose=EgressPurpose.PUBLICATION, publish=PublishGrant(REPO, "", "main", NOW + timedelta(minutes=5)))
    allowed = policy_check("publication", "GET", f"{API}/repos/{REPO}", None, probe, SourceSet(), NOW)
    assert allowed is None
    for method, path, body in [
        ("GET", f"/repos/{REPO}/git/ref/heads/main", None),
        ("POST", f"/repos/{REPO}/git/blobs", {"content": "x", "encoding": "base64"}),
        ("POST", f"/repos/{REPO}/git/refs", {"ref": "refs/heads/", "sha": BASE}),
        ("POST", f"/repos/{REPO}/pulls", {"head": "", "base": "main", "draft": True}),
        ("PATCH", f"/repos/{REPO}/pulls/1", {"body": "x"}),
    ]:
        reason = policy_check("publication", method, f"{API}{path}", body, probe, SourceSet(), NOW)
        assert reason == "an identity read may only read the repository itself", (method, path, reason)


def test_base_blobs_lists_files_and_the_push_uses_the_compared_base():
    remote = Remote()
    api = _api(remote)
    assert api.base_blobs(REPO, "main") == {"go.mod": "1" * 40, "netaddr/netaddr.go": "2" * 40}
    remote.refs["main"] = "f" * 40  # the base moves after the comparison
    remote.commits["f" * 40] = {"tree": TREE, "parents": [BASE], "message": "later"}
    commit = api.push_branch(REPO, GRANT.branch, "local-base", {"netaddr/netaddr.go": "package netaddr\n"}, "Fix")
    assert remote.commits[commit]["parents"] == [BASE]  # committed on the base that was compared


def test_tree_route_is_limited_to_the_approved_repository():
    ctx = RequestContext(purpose=EgressPurpose.PUBLICATION, publish=GRANT)
    assert policy_check("publication", "GET", f"{API}/repos/{REPO}/git/trees/{TREE}?recursive=1", None, ctx,
                        SourceSet(), NOW) is None
    assert policy_check("publication", "GET", f"{API}/repos/example/other/git/trees/{TREE}", None, ctx,
                        SourceSet(), NOW) == "repository does not match the approved destination"


def test_a_truncated_tree_is_not_compared():
    remote = Remote()
    remote.truncated = True
    with pytest.raises(PublicationUnavailable):
        _api(remote).base_blobs(REPO, "main")
