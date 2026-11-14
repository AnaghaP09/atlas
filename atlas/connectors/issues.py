"""Issue tracker and git connectors (GitHub)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from atlas.connectors import ReplayConnector
from atlas.connectors.github import GitHubError, GitHubReader
from atlas.connectors.normalize import commit_view, issue_as_of, issue_from_rest, pull_request_as_of, to_record
from atlas.contracts import Record, RunContext
from atlas.store import AUTHORITATIVE_WRITERS, EvidenceStore


class IssuesConnector(ReplayConnector):
    name = "issues"
    source = "issues"

    def __init__(self, store: EvidenceStore, reader: GitHubReader | None = None, **kw: Any) -> None:
        super().__init__(store, **kw)
        self.reader = reader

    def fetch_live(self, query: Mapping[str, Any], ctx: RunContext) -> list[Record]:
        """Poll one issue. A 304 revalidation or a successful fetch re-confirms the live record.

        A failed poll raises and confirms nothing, so the record ages normally.
        """
        if self.reader is None:
            raise RuntimeError("issues connector has no live reader")
        number = int(query["number"])
        writer = AUTHORITATIVE_WRITERS[self.source]
        current = self.store.current_live(self.source, f"issue/{number}")
        check = self.reader.rest_conditional(f"/issues/{number}")
        if check.unchanged and current is not None and not check.pending_refresh:
            confirmed = self.store.reconfirm(current.record_id, writer, self.clock(), method="not_modified")
            return [replace(current, confirmed_through=confirmed)]
        # Changed, unknown, or a newer version seen earlier was never stored: enrich and persist
        # it now. The validator is committed only after the version is stored, so a failure here
        # leaves the refresh pending and the stored record ages normally.
        try:
            raw = self.reader.issue(number)
        except GitHubError as exc:
            # The detailed history needs credentials. Without them, keep the live view current from
            # the REST representation, marked as having no edit history.
            if not exc.auth_or_limit or not isinstance(check.body, dict):
                raise
            stored = self.store_rest_issue(check.body, ctx)
            if stored:
                self.reader.commit_validator(check.url)
            return stored
        if raw is None:
            return []
        fetched = self.clock()
        asof = issue_as_of(raw, fetched)
        record, extra = to_record(self.source, asof, cutoff=fetched, fetched_at=fetched, history_complete_through=None, live=True)
        record_id = self.store.put(record, writer, **extra)
        confirmed = self.store.reconfirm(record_id, writer, fetched, method="fetch")
        self.reader.commit_validator(check.url)
        return [replace(record, record_id=record_id, confirmed_through=confirmed)]

    def store_rest_issue(self, rest: Mapping[str, Any], ctx: RunContext) -> list[Record]:
        """Store one issue from its REST representation in the live store (no edit history).

        Live polling only: the record is never complete history, so as-of snapshots and the
        evaluation never use it.
        """
        fetched = self.clock()
        asof = issue_from_rest(dict(rest), fetched)
        if asof is None:
            return []
        writer = AUTHORITATIVE_WRITERS[self.source]
        record, extra = to_record(self.source, asof, cutoff=fetched, fetched_at=fetched, history_complete_through=None, live=True)
        record_id = self.store.put(record, writer, **extra)
        confirmed = self.store.reconfirm(record_id, writer, fetched, method="fetch")
        return [replace(record, record_id=record_id, confirmed_through=confirmed)]


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
