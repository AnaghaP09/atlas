"""Issue tracker and git connectors (GitHub)."""

from __future__ import annotations

from typing import Any, Mapping

from atlas.connectors import ReplayConnector
from atlas.connectors.github import GitHubReader
from atlas.connectors.normalize import commit_view, issue_as_of, pull_request_as_of, to_record
from atlas.contracts import Record, RunContext
from atlas.store import AUTHORITATIVE_WRITERS, EvidenceStore


class IssuesConnector(ReplayConnector):
    name = "issues"
    source = "issues"

    def __init__(self, store: EvidenceStore, reader: GitHubReader | None = None, **kw: Any) -> None:
        super().__init__(store, **kw)
        self.reader = reader

    def fetch_live(self, query: Mapping[str, Any], ctx: RunContext) -> list[Record]:
        if self.reader is None:
            raise RuntimeError("issues connector has no live reader")
        raw = self.reader.issue(int(query["number"]))
        if raw is None:
            return []
        fetched = self.clock()
        asof = issue_as_of(raw, fetched)
        record, extra = to_record(self.source, asof, cutoff=fetched, fetched_at=fetched, history_complete_through=None, live=True)
        self.store.put(record, AUTHORITATIVE_WRITERS[self.source], **extra)
        return [record]


class GitConnector(ReplayConnector):
    name = "git"
    source = "git"

    def __init__(self, store: EvidenceStore, reader: GitHubReader | None = None, **kw: Any) -> None:
        super().__init__(store, **kw)
        self.reader = reader

    def fetch_live(self, query: Mapping[str, Any], ctx: RunContext) -> list[Record]:
        if self.reader is None:
            raise RuntimeError("git connector has no live reader")
        fetched = self.clock()
        if "pr" in query:
            raw = self.reader.pull_request(int(query["pr"]))
            asof = pull_request_as_of(raw, fetched) if raw else None
        elif "commit" in query:
            raw = self.reader.commit(str(query["commit"]))
            asof = commit_view(raw, fetched) if raw else None
        else:
            raise ValueError("git live fetch needs 'pr' or 'commit'")
        if asof is None:
            return []
        record, extra = to_record(self.source, asof, cutoff=fetched, fetched_at=fetched, history_complete_through=None, live=True)
        self.store.put(record, AUTHORITATIVE_WRITERS[self.source], **extra)
        return [record]
