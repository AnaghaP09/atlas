"""Turn raw source payloads into evidence records, as of a point in time.

The same code serves live ingestion (as of the fetch time) and snapshot preparation (as of a
case cutoff). Every mutable field is rebuilt from its own history; nothing is copied from the
current state into a historical view:

* body text from the content-edit history (each edit carries the full text at that time);
* title from rename events;
* open or closed state from close and reopen events;
* labels from label and unlabel events;
* cross-references, duplicates and closing links only from events dated before the cutoff.

When a field's history is incomplete, the field is excluded and listed in ``missing``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

from atlas.contracts import Provenance, Record
from atlas.freshness import reconstructed_confirmation
from atlas.store import AUTHORITATIVE_WRITERS, from_iso

TITLE_UNAVAILABLE = "title unavailable at cutoff"


def _t(value: str | None) -> datetime | None:
    return from_iso(value) if value else None


def content_hash(view: Any) -> str:
    blob = json.dumps(view, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


@dataclass
class AsOf:
    """A reconstructed view plus the times needed for the record."""

    source_id: str
    entity_type: str
    view: dict[str, Any]
    valid_from: datetime
    history_complete: bool
    missing: list[str] = field(default_factory=list)
    commit_sha: str | None = None


def text_at(
    current: str | None,
    created_at: datetime,
    edits: Iterable[dict[str, Any]],
    cutoff: datetime,
    *,
    edits_complete: bool = True,
) -> tuple[str | None, datetime | None]:
    """Body text as it stood at ``cutoff`` and the time it took that value.

    Each edit node carries the full text after the edit (``diff``) and ``editedAt``. The original
    text appears as the oldest edit once a body has been edited at least once.
    """
    if created_at > cutoff:
        return None, None
    versions = sorted(
        ((_t(e.get("editedAt") or e.get("createdAt")), e.get("diff")) for e in edits if not e.get("deletedAt")),
        key=lambda v: v[0],
    )
    versions = [(t, d) for t, d in versions if t is not None]
    if not versions:
        return current, created_at
    before = [(t, d) for t, d in versions if t <= cutoff]
    if before:
        t, d = before[-1]
        return (d if d is not None else None), t
    # Every recorded version is after the cutoff. The oldest recorded version is the original
    # only when the list is complete and it was written at creation time.
    oldest_t, oldest_d = versions[0]
    if edits_complete and abs((oldest_t - created_at).total_seconds()) <= 1:
        return oldest_d, created_at
    return None, None


def title_at(current: str, renames: list[dict[str, Any]], cutoff: datetime, complete: bool) -> str:
    if not complete:
        return TITLE_UNAVAILABLE
    later = sorted((r for r in renames if _t(r["createdAt"]) > cutoff), key=lambda r: r["createdAt"])
    return later[0]["previousTitle"] if later else current


def _ref_number(node: Any) -> int | None:
    if isinstance(node, dict):
        return node.get("number")
    return None


def issue_as_of(raw: dict[str, Any], cutoff: datetime) -> AsOf | None:
    """Reconstruct an issue (with its events) as it stood at ``cutoff``."""
    created = _t(raw["createdAt"])
    if created is None or created > cutoff:
        return None
    timeline = raw.get("timeline") or []
    timeline_complete = bool(raw.get("timeline_complete", True))
    events = sorted((e for e in timeline if e.get("createdAt") and _t(e["createdAt"]) <= cutoff), key=lambda e: e["createdAt"])
    missing: list[str] = []
    change_times = [created]

    body, body_t = text_at(raw.get("body"), created, (raw.get("userContentEdits") or {}).get("nodes", []), cutoff,
                           edits_complete=bool(raw.get("edits_complete", True)))
    if body is None:
        missing.append("body")
    elif body_t:
        change_times.append(body_t)

    renames = [e for e in timeline if e.get("__typename") == "RenamedTitleEvent"]
    title = title_at(raw["title"], renames, cutoff, timeline_complete)
    if title == TITLE_UNAVAILABLE:
        missing.append("title")

    state, state_reason, closed_by = "OPEN", None, None
    labels: set[str] = set()
    refs: list[dict[str, Any]] = []
    duplicate_of = None
    for e in events:
        kind = e["__typename"]
        t = _t(e["createdAt"])
        if kind == "ClosedEvent":
            state, state_reason = "CLOSED", e.get("stateReason")
            closer = e.get("closer") or {}
            closed_by = {"pr": closer.get("number")} if closer.get("__typename") == "PullRequest" else (
                {"commit": closer.get("oid")} if closer.get("__typename") == "Commit" else None)
            change_times.append(t)
        elif kind == "ReopenedEvent":
            state, state_reason, closed_by = "OPEN", None, None
            change_times.append(t)
        elif kind == "LabeledEvent":
            labels.add(e["label"]["name"])
            change_times.append(t)
        elif kind == "UnlabeledEvent":
            labels.discard(e["label"]["name"])
            change_times.append(t)
        elif kind == "RenamedTitleEvent":
            change_times.append(t)
        elif kind == "CrossReferencedEvent":
            src = e.get("source") or {}
            refs.append({"kind": "cross_reference", "type": src.get("__typename"), "number": src.get("number"),
                         "will_close": bool(e.get("willCloseTarget")), "at": e["createdAt"]})
        elif kind in ("ConnectedEvent", "DisconnectedEvent"):
            refs.append({"kind": "connected" if kind == "ConnectedEvent" else "disconnected",
                         "number": _ref_number(e.get("subject")), "at": e["createdAt"]})
        elif kind == "MarkedAsDuplicateEvent":
            duplicate_of = _ref_number(e.get("canonical"))
            change_times.append(t)
        elif kind == "UnmarkedAsDuplicateEvent":
            duplicate_of = None
            change_times.append(t)
        elif kind == "ReferencedEvent":
            refs.append({"kind": "commit_reference", "commit": (e.get("commit") or {}).get("oid"), "at": e["createdAt"]})
    if not timeline_complete:
        labels = set()
        state = "UNKNOWN"
        refs = []
        missing.extend(["labels", "state", "references"])

    comments = []
    for c in raw.get("comments_list") or []:
        ct = _t(c["createdAt"])
        if ct is None or ct > cutoff:
            continue
        ctext, ctime = text_at(c.get("body"), ct, (c.get("userContentEdits") or {}).get("nodes", []), cutoff)
        comments.append({"id": c.get("databaseId"), "created_at": c["createdAt"], "author": (c.get("author") or {}).get("login"),
                         "body": ctext, "body_missing": ctext is None})
        change_times.append(ctime or ct)
    if not raw.get("comments_complete", True):
        missing.append("comments")

    view = {
        "number": raw["number"],
        "title": title,
        "body": body,
        "state": state,
        "state_reason": state_reason,
        "closed_by": closed_by,
        "duplicate_of": duplicate_of,
        "labels": sorted(labels),
        "created_at": raw["createdAt"],
        "author": (raw.get("author") or {}).get("login"),
        "url": raw.get("url"),
        "references": refs,
        "comments": comments,
        "missing": sorted(set(missing)),
    }
    complete = timeline_complete and bool(raw.get("comments_complete", True)) and bool(raw.get("edits_complete", True))
    return AsOf(f"issue/{raw['number']}", "issue", view, max(t for t in change_times if t), complete, sorted(set(missing)))


def issue_from_rest(rest: dict[str, Any], fetched_at: datetime) -> AsOf | None:
    """A live issue built from the REST representation alone.

    Used only for the live store when the detailed history (edits, timeline, comments) cannot be
    read, for example without a GitHub token. The current state, title, labels and body are real;
    the edit history is marked unknown, so the record never counts as complete history and is never
    used for as-of snapshots or evaluation.
    """
    if not isinstance(rest, dict) or "number" not in rest or "pull_request" in rest:
        return None
    created = _t(rest.get("created_at"))
    if created is None or created > fetched_at:
        return None
    updated = _t(rest.get("updated_at")) or created
    labels = sorted(str(lab.get("name")) for lab in rest.get("labels", []) if isinstance(lab, dict) and lab.get("name"))
    missing = ["comments", "edit_history", "timeline"]
    view = {
        "number": rest["number"],
        "title": rest.get("title"),
        "body": rest.get("body"),
        "state": str(rest.get("state", "open")).upper(),
        "state_reason": rest.get("state_reason"),
        "closed_by": None,
        "duplicate_of": None,
        "labels": labels,
        "created_at": rest.get("created_at"),
        "updated_at": rest.get("updated_at"),
        "author": (rest.get("user") or {}).get("login"),
        "url": rest.get("html_url"),
        "references": [],
        "comments": [],
        "comment_count": rest.get("comments"),
        "edit_history": "unknown",
        "missing": missing,
    }
    return AsOf(f"issue/{rest['number']}", "issue", view, min(updated, fetched_at), False, missing)


def pull_request_as_of(raw: dict[str, Any], cutoff: datetime) -> AsOf | None:
    """A pull request as of ``cutoff``. Only pull requests merged before the cutoff are visible."""
    merged = _t(raw.get("mergedAt"))
    if merged is None or merged > cutoff:
        return None
    created = _t(raw["createdAt"])
    timeline = (raw.get("timelineItems") or {}).get("nodes", [])
    complete = len(timeline) < 50
    body, body_t = text_at(raw.get("body"), created, (raw.get("userContentEdits") or {}).get("nodes", []), cutoff)
    renames = [e for e in timeline if e.get("__typename") == "RenamedTitleEvent"]
    title = title_at(raw["title"], renames, cutoff, complete)
    labels: set[str] = set()
    for e in sorted(timeline, key=lambda x: x.get("createdAt", "")):
        if e.get("createdAt") and _t(e["createdAt"]) <= cutoff:
            if e["__typename"] == "LabeledEvent":
                labels.add(e["label"]["name"])
            elif e["__typename"] == "UnlabeledEvent":
                labels.discard(e["label"]["name"])
    missing = []
    if body is None:
        missing.append("body")
    if not complete:
        labels = set()
        missing.append("labels")
    files = (raw.get("files") or {}).get("nodes", [])
    view = {
        "number": raw["number"],
        "title": title,
        "body": body,
        "merged_at": raw["mergedAt"],
        "created_at": raw["createdAt"],
        "base": raw.get("baseRefName"),
        "author": (raw.get("author") or {}).get("login"),
        "merge_commit": (raw.get("mergeCommit") or {}).get("oid"),
        "labels": sorted(labels),
        "files": [f["path"] for f in files],
        "files_total": (raw.get("files") or {}).get("totalCount", len(files)),
        "url": raw.get("url"),
        "missing": missing,
    }
    times = [t for t in (created, merged, body_t) if t]
    return AsOf(f"pr/{raw['number']}", "pull_request", view, max(times), complete, missing,
                commit_sha=view["merge_commit"])


def commit_view(raw: dict[str, Any], cutoff: datetime) -> AsOf | None:
    committed = _t(raw.get("committed_at"))
    if committed is None or committed > cutoff:
        return None
    view = {
        "sha": raw["sha"],
        "summary": (raw.get("message") or "").splitlines()[0] if raw.get("message") else "",
        "message": raw.get("message", ""),
        "committed_at": raw["committed_at"],
        "author": raw.get("author"),
        "parents": raw.get("parents", []),
        "files": [f["path"] for f in raw.get("files", [])],
    }
    return AsOf(f"commit/{raw['sha']}", "commit", view, committed, True, [], commit_sha=raw["sha"])


def to_record(
    source: str,
    asof: AsOf,
    *,
    cutoff: datetime,
    fetched_at: datetime | None,
    history_complete_through: datetime | None,
    live: bool = False,
    scopes: frozenset[str] = frozenset({"public"}),
    provenance: Provenance = Provenance.OBSERVED,
    synthetic: bool = False,
) -> tuple[Record, dict[str, Any]]:
    """Build the record for an as-of view. Returns (record, extra store arguments)."""
    version = content_hash(asof.view)
    if live:
        confirmed = fetched_at
        basis = "live"
    elif source in ("git",) or asof.entity_type in ("commit",):
        confirmed = cutoff
        basis = "identity"
    else:
        confirmed = reconstructed_confirmation(cutoff, history_complete_through) if asof.history_complete else None
        basis = "reconstructed"
    record = Record(
        record_id=f"{source}:{asof.source_id}@{version}",
        source=source,
        source_id=asof.source_id,
        revision=None,
        content_version_id=version,
        valid_from=asof.valid_from,
        observed_at=asof.valid_from,
        confirmed_through=confirmed,
        writer=AUTHORITATIVE_WRITERS[source],
        provenance=provenance,
        synthetic_ancestry=synthetic,
        access_scopes=scopes,
        lineage=(),
        content_ref=f"db:{source}:{asof.source_id}@{version}",
        fetched_at=fetched_at,
    )
    extra = {"content": asof.view, "entity_type": asof.entity_type, "freshness_basis": basis, "commit_sha": asof.commit_sha}
    return record, extra
