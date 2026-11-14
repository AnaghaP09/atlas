"""GitHub reads for the issue and git connectors, always through the permission gateway."""

from __future__ import annotations

import re
from typing import Any

from atlas.connectors import github_queries as Q
from atlas.gateway import Gateway, GatewayRefused, RequestContext

API = "https://api.github.com"
_OP = re.compile(r"query\s+([A-Za-z_][A-Za-z0-9_]*)")


class GitHubError(RuntimeError):
    pass


class GitHubReader:
    """Paginated GitHub reads for one repository. Read-only by construction."""

    def __init__(self, gateway: Gateway, ctx: RequestContext, *, owner: str = "kubevirt", name: str = "kubevirt", connector: str = "issues") -> None:
        self.gateway = gateway
        self.ctx = ctx
        self.owner = owner
        self.name = name
        self.connector = connector

    # ------------------------------------------------------------------ transport helpers

    def graphql(self, document: str, variables: dict[str, Any]) -> dict[str, Any]:
        name = _OP.search(document).group(1)
        body = {"query": document, "variables": variables, "operationName": name}
        resp = self.gateway.request(self.connector, "POST", f"{API}/graphql", self.ctx, body)
        payload = resp.body if isinstance(resp.body, dict) else {}
        if resp.status != 200 or payload.get("errors"):
            raise GitHubError(f"GraphQL {name} failed with status {resp.status}: {str(payload.get('errors'))[:300]}")
        return payload["data"]

    def rest(self, path: str, params: dict[str, Any] | None = None) -> Any:
        query = ""
        if params:
            query = "?" + "&".join(f"{k}={v}" for k, v in params.items())
        resp = self.gateway.request(self.connector, "GET", f"{API}/repos/{self.owner}/{self.name}{path}{query}", self.ctx)
        if resp.status == 404:
            return None
        if resp.status != 200:
            raise GitHubError(f"GET {path} failed with status {resp.status}")
        return resp.body

    def _vars(self, **extra: Any) -> dict[str, Any]:
        return {"owner": self.owner, "name": self.name, **extra}

    # ------------------------------------------------------------------ issues

    def issue(self, number: int) -> dict[str, Any] | None:
        try:
            data = self.graphql(Q.ISSUE_SNAPSHOT, self._vars(number=number))
        except GitHubError as exc:
            if "Could not resolve" in str(exc) or "NOT_FOUND" in str(exc):
                return None
            raise
        issue = (data.get("repository") or {}).get("issue")
        if issue is None:
            return None
        edits = issue["userContentEdits"]
        issue["edits_complete"] = not edits["pageInfo"]["hasNextPage"]
        issue["timeline"], issue["timeline_complete"] = self._paged(Q.ISSUE_TIMELINE, number, "timelineItems")
        issue["comments_list"], issue["comments_complete"] = self._paged(Q.ISSUE_COMMENTS, number, "comments")
        return issue

    def _paged(self, document: str, number: int, field: str, limit_pages: int = 30) -> tuple[list[dict[str, Any]], bool]:
        nodes: list[dict[str, Any]] = []
        after = None
        for _ in range(limit_pages):
            data = self.graphql(document, self._vars(number=number, after=after))
            conn = data["repository"]["issue"][field]
            nodes.extend(conn["nodes"])
            if not conn["pageInfo"]["hasNextPage"]:
                return nodes, True
            after = conn["pageInfo"]["endCursor"]
        return nodes, False

    def search_numbers(self, query: str, limit: int = 1000) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        after = None
        while len(out) < limit:
            data = self.graphql(Q.ISSUE_SEARCH, {"q": query, "after": after})
            conn = data["search"]
            out.extend(n for n in conn["nodes"] if n)
            if not conn["pageInfo"]["hasNextPage"]:
                break
            after = conn["pageInfo"]["endCursor"]
        return out[:limit]

    # ------------------------------------------------------------------ pull requests and commits

    def pull_request(self, number: int) -> dict[str, Any] | None:
        try:
            data = self.graphql(Q.PULL_REQUEST_SNAPSHOT, self._vars(number=number))
        except GitHubError as exc:
            if "Could not resolve" in str(exc) or "NOT_FOUND" in str(exc):
                return None
            raise
        return (data.get("repository") or {}).get("pullRequest")

    def commit(self, sha: str) -> dict[str, Any] | None:
        body = self.rest(f"/commits/{sha}")
        if body is None:
            return None
        return {
            "sha": body.get("sha"),
            "message": (body.get("commit") or {}).get("message", ""),
            "committed_at": ((body.get("commit") or {}).get("committer") or {}).get("date"),
            "authored_at": ((body.get("commit") or {}).get("author") or {}).get("date"),
            "author": (body.get("author") or {}).get("login"),
            "parents": [p.get("sha") for p in body.get("parents", [])],
            "files": [{"path": f.get("filename"), "additions": f.get("additions"), "deletions": f.get("deletions")} for f in body.get("files", [])][:300],
        }

    def tag_commit_time(self, tag: str) -> dict[str, Any] | None:
        ref = self.rest(f"/git/ref/tags/{tag}")
        if ref is None:
            return None
        obj = ref["object"]
        sha = obj["sha"]
        if obj["type"] == "tag":
            tag_obj = self.rest(f"/git/tags/{sha}")
            tagged_at = (tag_obj.get("tagger") or {}).get("date")
            sha = tag_obj["object"]["sha"]
        else:
            tagged_at = None
        commit = self.rest(f"/commits/{sha}")
        committed = ((commit or {}).get("commit") or {}).get("committer", {}).get("date")
        return {"tag": tag, "commit": sha, "tagged_at": tagged_at or committed, "committed_at": committed}


def safe_reader(gateway: Gateway, ctx: RequestContext, **kwargs: Any) -> GitHubReader:
    return GitHubReader(gateway, ctx, **kwargs)


__all__ = ["GitHubReader", "GitHubError", "GatewayRefused", "safe_reader"]
