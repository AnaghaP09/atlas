"""Publication to the target repository on GitHub, through the permission gateway.

Every request carries the task's publish grant (repository, branch, base and approval expiry),
and the gateway refuses anything outside it. Writes are sent once; the publisher's journal handles
an unclear outcome by reading the remote state before any retry.
"""

from __future__ import annotations

import base64
from typing import Any, Callable, Mapping
from urllib.parse import quote

from atlas.contracts import EgressPurpose, PullRequest, RepoIdentity
from atlas.gateway import Gateway, PublishGrant, RequestContext

API = "https://api.github.com"


class PublicationUnavailable(RuntimeError):
    pass


class GitHubPublicationAPI:
    def __init__(self, gateway: Gateway, grant_for: Callable[[str], PublishGrant | None]) -> None:
        self.gateway = gateway
        self.grant_for = grant_for

    def _call(self, method: str, full_name: str, path: str, body: Any = None, *, missing_ok: bool = False) -> Any:
        grant = self.grant_for(full_name)
        if grant is None:
            raise PublicationUnavailable(f"no approved publication for {full_name}")
        ctx = RequestContext(purpose=EgressPurpose.PUBLICATION, publish=grant)
        response = self.gateway.request("publication", method, f"{API}/repos/{full_name}{path}", ctx, body)
        if response.status == 404 and missing_ok:
            return None
        if response.status >= 300:
            raise PublicationUnavailable(f"{method} {path} answered {response.status}")
        return response.body

    def repo_identity(self, full_name: str) -> RepoIdentity:
        body = self._call("GET", full_name, "")
        return RepoIdentity(str(body["node_id"]), str(body["full_name"]))

    def branch_head(self, full_name: str, branch: str) -> str | None:
        body = self._call("GET", full_name, f"/git/ref/heads/{quote(branch)}", missing_ok=True)
        return str(body["object"]["sha"]) if body else None

    def push_branch(self, full_name: str, branch: str, base_commit: str, files: Mapping[str, str | None],
                    message: str) -> str:
        """Create ``branch`` with one commit on the head of the base branch. ``base_commit`` is ATLAS's
        id for the base tree it prepared against; the remote base is read from the base branch."""
        grant = self.grant_for(full_name)
        if grant is None:
            raise PublicationUnavailable(f"no approved publication for {full_name}")
        base_sha = self.branch_head(full_name, grant.base_ref)
        if base_sha is None:
            raise PublicationUnavailable(f"base branch {grant.base_ref} not found")
        base_tree = self._call("GET", full_name, f"/git/commits/{base_sha}")["tree"]["sha"]
        entries = []
        for path, content in sorted(files.items()):
            if content is None:
                entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
                continue
            encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
            blob = self._call("POST", full_name, "/git/blobs", {"content": encoded, "encoding": "base64"})
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        tree = self._call("POST", full_name, "/git/trees", {"base_tree": base_tree, "tree": entries})
        commit = self._call("POST", full_name, "/git/commits",
                            {"message": message, "tree": tree["sha"], "parents": [base_sha]})
        self._call("POST", full_name, "/git/refs", {"ref": f"refs/heads/{branch}", "sha": commit["sha"]})
        return str(commit["sha"])

    @staticmethod
    def _pull(body: Mapping[str, Any]) -> PullRequest:
        return PullRequest(int(body["number"]), str(body["head"]["ref"]), str(body["base"]["ref"]),
                           str(body.get("title") or ""), str(body.get("body") or ""),
                           bool(body.get("draft", False)), str(body.get("state") or "open"))

    def find_pulls(self, full_name: str, head: str) -> list[PullRequest]:
        owner = full_name.split("/")[0]
        body = self._call("GET", full_name, f"/pulls?head={quote(owner)}:{quote(head)}&state=all") or []
        return [self._pull(p) for p in body if p["head"]["ref"] == head]

    def create_draft_pull(self, full_name: str, head: str, base: str, title: str, body: str) -> PullRequest:
        created = self._call("POST", full_name, "/pulls",
                             {"title": title, "head": head, "base": base, "body": body, "draft": True})
        return self._pull(created)

    def get_pull(self, full_name: str, number: int) -> PullRequest:
        return self._pull(self._call("GET", full_name, f"/pulls/{int(number)}"))

    def replace_pull_body(self, full_name: str, number: int, body: str) -> None:
        self._call("PATCH", full_name, f"/pulls/{int(number)}", {"body": body})
