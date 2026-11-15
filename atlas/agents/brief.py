"""The evidence brief: the records linked to one event, gathered by code before the agent starts.

An investigation always needs the same first reads: the reported issue with its comments, the issues,
pull requests and commits it points to, the logs of its failing jobs, other reports of the same test,
the changes merged shortly before the report, and cluster events. Gathering them in code makes every
run start from the same evidence, and it defines which records are *linked* to the event: a linked
record that carries instruction-like text holds the run whether or not the agent would have read it.
Records elsewhere in the data set are not linked and do not hold the run.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Mapping

from atlas.agents.resolver import EvidenceResolver, ResolvedRecord

ISSUE_CHARS = 12_000
LINKED_CHARS = 3_500
LOG_CHARS = 5_000
MAX_LINKED = 8
MAX_RELATED = 15
MAX_MERGES = 60
MERGE_WINDOW = timedelta(days=21)

_NUMBER = re.compile(r"(?:#|/pull/|/issues/)(\d{3,7})\b")
_COMMIT = re.compile(r"(?:/commit/|\bcommit\s+)([0-9a-f]{7,40})\b")
_TEST_ID = re.compile(r"test_id:\s*(\d+)")
_JOB = re.compile(r"\b((?:pull|periodic|ci|postsubmit)-[A-Za-z0-9._-]{6,})")
_BUILD = re.compile(r"/(\d{12,})\b")
_BLOCKER = re.compile(r"(?i)release[-_ ]blocker")
_DUPLICATE = re.compile(r"(?i)(?:in favou?r of|duplicate of|dup(?:licate)? of|same as|tracked (?:in|by))\s+(?:https?://\S*/issues/|#)(\d{3,7})")
_LOG_SIGNAL = re.compile(r"(?i)(fail|error|panic|timeout|timed out|fatal|assert|exception|refused|killed|oom)")


@dataclass
class Brief:
    text: str
    event_record_id: str | None
    linked_ids: list[str] = field(default_factory=list)
    flagged_ids: list[str] = field(default_factory=list)
    release_blocker: bool = False
    duplicate_candidates: list[str] = field(default_factory=list)


def display_id(record_id: str) -> str:
    """A record id as shown to the agent. The stored id has an "@" before its version, which the
    outbound boundary would treat as a personal handle; "~" is shown instead."""
    return record_id.replace("@", "~")


def canonical_id(shown: str, known: Mapping[str, Any]) -> str:
    """Map an id the agent cited back to a stored record id (exact, "~" form, or unique prefix)."""
    shown = str(shown).strip().strip("[]")
    if shown.startswith("record "):
        shown = shown[len("record "):]
    if shown in known:
        return shown
    swapped = shown.replace("~", "@")
    if swapped in known:
        return swapped
    stem = swapped.split("@")[0]
    matches = [k for k in known if k.split("@")[0] == stem]
    return matches[0] if len(matches) == 1 else shown


def _meta(rec: ResolvedRecord | None) -> dict[str, Any]:
    """Dates and numbers of a record, read even when its text is withheld from the agent."""
    if rec is None or not rec.content:
        return {}
    try:
        doc = json.loads(rec.content)
    except ValueError:
        return {}
    return doc if isinstance(doc, dict) else {}


def _doc(rec: ResolvedRecord | None) -> dict[str, Any]:
    if rec is None or rec.status != "available" or not rec.content:
        return {}
    try:
        doc = json.loads(rec.content)
    except ValueError:
        return {"body": rec.content}
    return doc if isinstance(doc, dict) else {"body": rec.content}


def _cut(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + f"\n[cut at {limit} of {len(text)} characters; read_record gives the rest]"


def _issue_text(doc: Mapping[str, Any], limit: int) -> str:
    lines = [f"title: {doc.get('title')}", f"state: {doc.get('state')}  labels: {', '.join(doc.get('labels') or []) or 'none'}",
             f"opened: {doc.get('created_at')}"]
    if doc.get("duplicate_of"):
        lines.append(f"marked duplicate of: {doc.get('duplicate_of')}")
    lines.append("body:\n" + str(doc.get("body") or ""))
    for c in doc.get("comments") or []:
        lines.append(f"comment by {c.get('author')} at {c.get('created_at')}:\n{c.get('body') or ''}")
    for ref in doc.get("references") or []:
        lines.append("reference: " + json.dumps(ref, sort_keys=True))
    return _cut("\n".join(lines), limit)


def _log_tail(log: str) -> str:
    hits = [ln for ln in log.splitlines() if _LOG_SIGNAL.search(ln)]
    out: list[str] = []
    size = 0
    for ln in reversed(hits):
        ln = ln[:400]
        if size + len(ln) > LOG_CHARS:
            break
        out.append(ln)
        size += len(ln) + 1
    return "\n".join(reversed(out))


def _parse_time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def build_brief(resolver: EvidenceResolver, event_id: str) -> Brief:
    """Gather the records linked to the event and render them as data for the agent."""
    linked: list[str] = []
    flagged: list[str] = []
    parts: list[str] = []

    def note(rec: ResolvedRecord | None) -> ResolvedRecord | None:
        if rec is None:
            return None
        if rec.record_id not in linked:
            linked.append(rec.record_id)
        if rec.flags and rec.record_id not in flagged:
            flagged.append(rec.record_id)
        return rec

    def header(rec: ResolvedRecord) -> str:
        extra = "" if rec.status == "available" else f" status={rec.status} ({rec.reason})"
        return f"[record {display_id(rec.record_id)}{extra}]"

    number = str(event_id).split("/")[-1]
    event = note(resolver.get("issues", f"issue/{number}") or resolver.get("issues", str(event_id)))
    if event is None:
        return Brief(f"No issue {event_id} exists in this data set.", None)
    doc = _doc(event)
    event_text = json.dumps(doc, default=str)
    parts.append("## The reported issue\n" + header(event) + "\n" + (_issue_text(doc, ISSUE_CHARS) if doc else "content not readable"))

    # Records the issue points to: issues, pull requests and commits.
    numbers: list[str] = []
    for ref in doc.get("references") or []:
        if ref.get("number"):
            numbers.append(str(ref["number"]))
    numbers += _NUMBER.findall(event_text)
    seen_numbers: list[str] = []
    for n in numbers:
        if n != number and n not in seen_numbers:
            seen_numbers.append(n)
    linked_docs: list[tuple[ResolvedRecord, dict[str, Any], str]] = []
    absent: list[str] = []
    for n in seen_numbers:
        rec = resolver.get("issues", f"issue/{n}") or resolver.get("git", f"pr/{n}")
        if rec is None:
            absent.append(n)
            continue
        note(rec)
        linked_docs.append((rec, _doc(rec), "pull request" if rec.record.source == "git" else "issue"))
    commits = {str(r.get("commit")) for r in (doc.get("references") or []) if r.get("commit")} | set(_COMMIT.findall(event_text))
    for _, ldoc, _kind in list(linked_docs):
        commits |= {str(r.get("commit")) for r in (ldoc.get("references") or []) if r.get("commit")}
        commits |= set(_COMMIT.findall(json.dumps(ldoc, default=str)))
    commit_recs: list[ResolvedRecord] = []
    for sha in sorted(commits):
        for rec in resolver.search({"source": "git", "entity_type": "commit", "text": sha, "limit": 3}):
            if rec.record.source_id.split("/")[-1].startswith(sha[:7]):
                commit_recs.append(note(rec))  # type: ignore[arg-type]
    if linked_docs or commit_recs or absent:
        block = ["## Records the issue points to"]
        for rec, ldoc, kind in linked_docs[:MAX_LINKED]:
            if not ldoc:
                block.append(f"{header(rec)} {kind}: content withheld or unavailable")
            elif kind == "issue":
                block.append(f"{header(rec)} issue {ldoc.get('number')}\n" + _issue_text(ldoc, LINKED_CHARS))
            else:
                files = ", ".join(str(f.get("path", f)) if isinstance(f, Mapping) else str(f) for f in (ldoc.get("files") or [])[:12])
                block.append(f"{header(rec)} pull request {ldoc.get('number')}: {ldoc.get('title')}\nmerged: {ldoc.get('merged_at')}  "
                             f"labels: {', '.join(ldoc.get('labels') or []) or 'none'}\nfiles ({ldoc.get('files_total')}): {files}\n"
                             + _cut(str(ldoc.get("body") or ""), LINKED_CHARS))
        for rec in commit_recs[:MAX_LINKED]:
            cdoc = _doc(rec)
            files = ", ".join(str(f.get("path", f)) if isinstance(f, Mapping) else str(f) for f in (cdoc.get("files") or [])[:12])
            block.append(f"{header(rec)} commit {str(cdoc.get('sha'))[:12]} of pull request {cdoc.get('pull_request')} "
                         f"({cdoc.get('committed_at')}): {_cut(str(cdoc.get('summary') or ''), 600)}\nfiles: {files}")
        if absent:
            block.append("Mentioned but not in this data set: " + ", ".join("#" + n for n in absent[:20]))
        parts.append("\n".join(block))

    # Job logs of the failing runs.
    builds = set(_BUILD.findall(event_text))
    jobs = set(_JOB.findall(event_text))
    ci = resolver.search({"source": "ci", "limit": 50})
    named = [r for r in ci if any(b in r.record.source_id for b in builds)] or \
            [r for r in ci if any(j in r.record.source_id for j in jobs)] or ci
    if named:
        block = ["## Job logs (lines that carry a failure signal, newest last)"]
        for rec in named[:3]:
            note(rec)
            jdoc = _doc(rec)
            if not jdoc:
                block.append(f"{header(rec)} log withheld or unavailable")
                continue
            log = str(jdoc.get("log") or "")
            block.append(f"{header(rec)} job {jdoc.get('job')} build {jdoc.get('build_id')} result {jdoc.get('result')} "
                         f"finished {jdoc.get('finished_at')} lines {jdoc.get('line_count')}\n"
                         + (_log_tail(log) if log else "no log text was captured for this job"))
        parts.append("\n".join(block))
    else:
        parts.append("## Job logs\nNo job log is in this data set for this event.")

    # Other issues that report the same test or job.
    related: dict[str, ResolvedRecord] = {}
    for key in [f"test_id:{t}" for t in _TEST_ID.findall(event_text)][:4] + sorted(jobs)[:3]:
        for rec in resolver.search({"source": "issues", "text": key, "limit": 30}):
            if rec.record_id != event.record_id:
                related.setdefault(rec.record_id, rec)
    if related:
        block = ["## Other issues that mention the same test or job"]
        for rec in list(related.values())[:MAX_RELATED]:
            note(rec)
            rdoc = _doc(rec)
            block.append(f"{header(rec)} #{rdoc.get('number')} [{rdoc.get('state')}] opened {rdoc.get('created_at')} "
                         f"labels: {', '.join(rdoc.get('labels') or []) or 'none'}: {str(rdoc.get('title'))[:200]}")
        parts.append("\n".join(block))

    # Changes merged shortly before the report.
    cutoff = resolver.ctx.cutoff
    merges: list[tuple[datetime, ResolvedRecord, dict[str, Any]]] = []
    for rec in resolver.search({"source": "git", "limit": 1000}):
        mdoc = _doc(rec)
        meta = _meta(rec)
        when = _parse_time(meta.get("merged_at") or meta.get("committed_at"))
        try:
            recent = when is not None and cutoff is not None and cutoff - MERGE_WINDOW <= when <= cutoff
        except TypeError:
            recent = when is not None
        if recent:
            note(rec)  # every recent change is linked: its text is scanned even when it is not listed
            if rec.record.source_id.startswith("pr/") and when is not None:
                merges.append((when, rec, mdoc))
    if merges:
        block = ["## Pull requests merged in the three weeks before the report (newest first)"]
        for when, rec, mdoc in sorted(merges, key=lambda m: m[0], reverse=True)[:MAX_MERGES]:
            if not mdoc:
                block.append(f"{header(rec)} content withheld")
                continue
            dirs = sorted({"/".join(str(f.get("path", f) if isinstance(f, Mapping) else f).split("/")[:2]) for f in (mdoc.get("files") or [])})[:4]
            block.append(f"{header(rec)} #{mdoc.get('number')} merged {when.date()} | {str(mdoc.get('title'))[:140]} | {', '.join(dirs)}")
        parts.append("\n".join(block))

    events = resolver.search({"source": "kubernetes", "limit": 20})
    if events:
        block = ["## Cluster events around the failure"]
        for rec in events:
            note(rec)
            block.append(f"{header(rec)} provenance={rec.record.provenance} " + _cut(rec.content or "content withheld", 500))
        parts.append("\n".join(block))

    dup = [n for n in ([str(doc.get("duplicate_of"))] if doc.get("duplicate_of") else []) + _DUPLICATE.findall(event_text) if n != number]
    blocker = bool(_BLOCKER.search(" ".join(doc.get("labels") or []) + " " + str(doc.get("title") or "") + " " + str(doc.get("body") or "")
                                   + " " + " ".join(str(c.get("body") or "") for c in doc.get("comments") or [])))
    if flagged:
        parts.append("## Withheld records\n" + str(len(flagged)) + " linked record(s) contain instruction-like text and were withheld.")
    return Brief("\n\n".join(parts), event.record_id, linked, flagged, blocker, list(dict.fromkeys(dup)))
