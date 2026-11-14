"""Cached raw fetches through the permission gateway, with a manifest of every request.

Raw payloads are written only below ``data/local/raw`` (never committed). Every request made is
listed in the evaluator manifest with its time and the SHA256 of the response.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from atlas.connectors.github import GitHubReader
from atlas.contracts import EgressPurpose, Mode, RunMode
from atlas.gateway import Gateway, GatewayRefused, RequestContext, Response, httpx_transport

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
RAW = DATA / "local" / "raw"
MANIFEST = DATA / "evaluator" / "MANIFEST.json"

PROW_VIEW = re.compile(r"https?://prow\.ci\.kubevirt\.io/+view/gs/kubevirt-prow/([A-Za-z0-9._~/-]+?)(?=[)\]\s>\"'`]|$)")


def now() -> datetime:
    return datetime.now(timezone.utc)


def _sha(obj: Any) -> str:
    blob = obj if isinstance(obj, (bytes, bytearray)) else json.dumps(obj, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


class Manifest:
    def __init__(self, path: Path = MANIFEST) -> None:
        self.path = path
        self.entries: dict[str, Any] = {}
        if path.exists():
            self.entries = json.loads(path.read_text(encoding="utf-8"))

    def add(self, key: str, request: dict[str, Any], response_sha256: str, fetched_at: datetime) -> None:
        self.entries[key] = {"request": request, "fetched_at": fetched_at.isoformat(), "response_sha256": response_sha256}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.entries, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def live_gateway(transport: Callable | None = None) -> Gateway:
    token = os.environ.get("GITHUB_TOKEN", "")
    return Gateway(
        transport or httpx_transport(),
        mode=lambda: Mode.CONNECTED,
        tokens={"github": lambda: token},
    )


LIVE_CTX = RequestContext(purpose=EgressPurpose.LIVE_INGEST, run_mode=RunMode.LIVE)


class RawCache:
    """Fetch once, then serve from ``data/local/raw``."""

    def __init__(self, gateway: Gateway | None = None, manifest: Manifest | None = None, raw_dir: Path = RAW) -> None:
        self.gateway = gateway or live_gateway()
        self.manifest = manifest or Manifest()
        self.raw = raw_dir
        self.issues_reader = GitHubReader(self.gateway, LIVE_CTX, connector="issues")
        self.git_reader = GitHubReader(self.gateway, LIVE_CTX, connector="git")

    def _cached(self, kind: str, key: str, fetch: Callable[[], Any], request: dict[str, Any]) -> Any:
        path = self.raw / kind / f"{key}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        fetched_at = now()
        value = fetch()
        envelope = {"fetched_at": fetched_at.isoformat(), "request": request, "data": value}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(envelope, sort_keys=True), encoding="utf-8")
        self.manifest.add(f"{kind}/{key}", request, _sha(value), fetched_at)
        return envelope

    def issue(self, number: int) -> dict[str, Any]:
        return self._cached("issues", str(number), lambda: self.issues_reader.issue(number),
                            {"connector": "issues", "operation": "IssueSnapshot+IssueTimeline+IssueComments", "number": number})

    def pull_request(self, number: int) -> dict[str, Any]:
        return self._cached("pulls", str(number), lambda: self.git_reader.pull_request(number),
                            {"connector": "git", "operation": "PullRequestSnapshot", "number": number})

    def commit(self, sha: str) -> dict[str, Any]:
        return self._cached("commits", sha, lambda: self.git_reader.commit(sha), {"connector": "git", "path": f"/commits/{sha}"})

    def search(self, key: str, query: str, limit: int = 1000) -> dict[str, Any]:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", key)
        return self._cached("search", safe, lambda: self.issues_reader.search_numbers(query, limit),
                            {"connector": "issues", "operation": "IssueSearch", "query": query})

    def tag(self, tag: str) -> dict[str, Any]:
        return self._cached("tags", tag, lambda: self.git_reader.tag_commit_time(tag), {"connector": "git", "tag": tag})

    def prow_artifact(self, prow_path: str, name: str, max_lines: int = 50_000) -> dict[str, Any]:
        """Fetch a Prow job artefact (build log or job metadata) from public storage."""
        key = hashlib.sha256(f"{prow_path}/{name}".encode()).hexdigest()[:24]
        url = f"https://storage.googleapis.com/kubevirt-prow/{prow_path.strip('/')}/{name}"

        def fetch() -> Any:
            try:
                resp: Response = self.gateway.request("ci", "GET", url, LIVE_CTX)
            except GatewayRefused as exc:
                return {"status": "refused", "reason": exc.reason}
            if resp.status != 200:
                return {"status": "unavailable", "http_status": resp.status}
            text = resp.body if isinstance(resp.body, str) else json.dumps(resp.body)
            lines = text.splitlines()
            truncated = len(lines) > max_lines
            kept = lines[-max_lines:] if truncated else lines
            return {"status": "ok", "line_count": len(lines), "truncated": truncated, "text": "\n".join(kept)}

        return self._cached("prow", key, fetch, {"connector": "ci", "url": url})


def prow_paths(text: str | None) -> list[str]:
    if not text:
        return []
    seen: list[str] = []
    for m in PROW_VIEW.finditer(text):
        p = m.group(1).strip("/")
        if p not in seen:
            seen.append(p)
    return seen
