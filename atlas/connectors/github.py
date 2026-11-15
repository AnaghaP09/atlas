"""GitHub reads for the issue and git connectors, always through the permission gateway."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from atlas.connectors import github_queries as Q
from atlas.gateway import Gateway, GatewayRefused, RequestContext

API = "https://api.github.com"
_OP = re.compile(r"query\s+([A-Za-z_][A-Za-z0-9_]*)")


class GitHubError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    @property
    def auth_or_limit(self) -> bool:
        """True when the request was refused for missing credentials or a rate limit."""
        return self.status in (401, 403, 429)


@dataclass(frozen=True)
class RestResult:
    body: Any
    unchanged: bool
    url: str
    pending_refresh: bool = False
    validator_pending: bool = False


class GitHubReader:
    """Paginated GitHub reads for one repository. Read-only by construction."""

    def __init__(self, gateway: Gateway, ctx: RequestContext, *, owner: str, name: str, connector: str = "issues") -> None:
        self.gateway = gateway
        self.ctx = ctx
        self.owner = owner
        self.name = name
        self.connector = connector
        self.validators: dict[str, dict[str, Any]] = {}
        self.pending_validators: dict[str, dict[str, Any]] = {}
        self.rate_remaining: int | None = None
        self.rate_reset: int | None = None

    def _note_rate(self, headers: Any) -> None:
        h = {str(k).lower(): v for k, v in (headers or {}).items()}
        try:
            if "x-ratelimit-remaining" in h:
                self.rate_remaining = int(h["x-ratelimit-remaining"])
            if "x-ratelimit-reset" in h:
                self.rate_reset = int(h["x-ratelimit-reset"])
        except (TypeError, ValueError):
            pass

    # ------------------------------------------------------------------ transport helpers

    def graphql(self, document: str, variables: dict[str, Any]) -> dict[str, Any]:
        name = _OP.search(document).group(1)
        body = {"query": document, "variables": variables, "operationName": name}
        resp = self.gateway.request(self.connector, "POST", f"{API}/graphql", self.ctx, body)
        self._note_rate(resp.headers)
        payload = resp.body if isinstance(resp.body, dict) else {}
        if resp.status != 200 or payload.get("errors"):
            raise GitHubError(f"GraphQL {name} failed with status {resp.status}: {str(payload.get('errors'))[:300]}",
                              status=resp.status)
        return payload["data"]

    def rest(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Plain GET. Its validator is committed at once (nothing else depends on it)."""
        result = self.rest_conditional(path, params)
        if result.validator_pending:
            self.commit_validator(result.url)
        return result.body

    def rest_conditional(self, path: str, params: dict[str, Any] | None = None) -> "RestResult":
        """GET with HTTP revalidation.

        Validators are two-stage: a validator seen in a 200 response stays *pending* until the
        caller has enriched and persisted that exact version and calls ``commit_validator``. A
        bodyless ``304 Not Modified`` is ``unchanged``; when it answers a pending validator the
        result says ``pending_refresh`` so the caller retries enrichment instead of re-confirming
        what it already stored. A 304 with no known validator is an error; 404 returns no body.
        """
        query = ""
        if params:
            query = "?" + "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{API}/repos/{self.owner}/{self.name}{path}{query}"
        pending = self.pending_validators.get(url)
        known = pending or self.validators.get(url)
        conditional: dict[str, str] = {}
        if known:
            if known.get("etag"):
                conditional["If-None-Match"] = known["etag"]
            if known.get("last_modified"):
                conditional["If-Modified-Since"] = known["last_modified"]
        resp = self.gateway.request(self.connector, "GET", url, self.ctx, conditional=conditional or None)
        self._note_rate(resp.headers)
        if resp.status == 304:
            if not known:
                raise GitHubError(f"GET {path} returned 304 without a cached body")
            return RestResult(known["body"], True, url, pending_refresh=pending is not None, validator_pending=pending is not None)
        if resp.status == 404:
            return RestResult(None, False, url)
        if resp.status != 200:
            raise GitHubError(f"GET {path} failed with status {resp.status}", status=resp.status)
        headers = {k.lower(): v for k, v in (resp.headers or {}).items()}
        if headers.get("etag") or headers.get("last-modified"):
            self.pending_validators[url] = {"etag": headers.get("etag"), "last_modified": headers.get("last-modified"), "body": resp.body}
            return RestResult(resp.body, False, url, validator_pending=True)
        return RestResult(resp.body, False, url)

    def commit_validator(self, url: str) -> None:
        """The version behind the pending validator has been enriched and persisted."""
        pending = self.pending_validators.pop(url, None)
        if pending is not None:
            self.validators[url] = pending

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

    def component_churn(self, ref: str, since: str, until: str, paths: list[str], batch: int = 40) -> dict[str, int]:
        """Commits touching each path between ``since`` and ``until`` on the history of ``ref``."""
        out: dict[str, int] = {}
        for start in range(0, len(paths), batch):
            chunk = paths[start:start + batch]
            data = self.graphql(Q.component_churn(chunk), self._vars(ref=ref, since=since, until=until))
            obj = ((data.get("repository") or {}).get("object")) or {}
            for n, path in enumerate(chunk):
                out[path] = int((obj.get(f"c{n}") or {}).get("totalCount") or 0)
        return out

    def commit_before(self, branch: str, until: str) -> str | None:
        """The newest commit on ``branch`` at or before ``until`` (ISO time)."""
        body = self.rest("/commits", {"sha": branch, "until": until, "per_page": 1})
        return body[0]["sha"] if isinstance(body, list) and body else None

    def file_text(self, path: str, ref: str | None = None) -> tuple[str, str] | None:
        """Text of one file at ``ref`` (default branch when omitted). Returns (text, blob sha)."""
        body = self.rest(f"/contents/{path}", {"ref": ref} if ref else None)
        if not isinstance(body, dict) or body.get("encoding") != "base64":
            return None
        return _b64(body.get("content") or ""), str(body.get("sha") or "")

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


def _b64(text: str) -> str:
    import base64

    return base64.b64decode(text).decode("utf-8", errors="replace")


def safe_reader(gateway: Gateway, ctx: RequestContext, **kwargs: Any) -> GitHubReader:
    return GitHubReader(gateway, ctx, **kwargs)


__all__ = ["GitHubReader", "GitHubError", "GatewayRefused", "safe_reader"]
