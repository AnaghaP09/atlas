"""Cleaning and checks shared by every frozen "Data as of" copy, offline or built in the server.

* People: names, handles and email addresses become stable pseudonyms (``person-<hash>``); the
  salt lives only in the private data directory. Bots and organisation accounts stay readable.
* Secrets and noise in text: signed URL parameters, tokens, SSH keys and embedded images are removed.
* Leakage: nothing in a copy may be dated after its cut-off (record versions, relationships,
  comments).
* Export: ``records.jsonl``, ``conflicts.jsonl`` and ``snapshot.json``, written so the registry can
  build the same read-only file every time.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from atlas.store import EvidenceDB, from_iso

MENTION = re.compile(r"(?<![A-Za-z0-9_/.-])@([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))(?![A-Za-z0-9_-])")
ENCODED_DATA_URI = re.compile(r"data(?::|%3A)[A-Za-z0-9.+/%-]+?(?:;|%3B)base64(?:,|%2C)[A-Za-z0-9+/=%]+", re.I)
SIGNED_PARAM = re.compile(r"([?&](?:jwt|token|sig|signature|X-Amz-Signature|X-Goog-Signature)=)[^&\s\"'<>)\]]+", re.I)
JWT = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
DATA_URI = re.compile(r"data:[A-Za-z0-9.+/-]+;base64,[A-Za-z0-9+/=]+")
SSH_KEY = re.compile(r"\b(ssh-(?:rsa|ed25519|dss)|ecdsa-sha2-nistp\d+) AAAA[A-Za-z0-9+/=]{20,}")
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
# Bots and organisation accounts are not people; keep them so the evidence stays readable.
GENERIC_BOTS = frozenset({"dependabot", "dependabot[bot]", "github-actions", "github-actions[bot]", "copilot",
                          "renovate", "renovate[bot]", "pre-commit-ci", "pre-commit-ci[bot]"})
# Words that look like mentions in technical text but are not accounts.
NOT_HANDLES = frozenset({"example", "types", "param", "return", "todo", "v1", "v2", "latest", "main", "host"})
PERSON_KEYS = ("author", "editor", "login", "actor", "assignee")


class LeakageError(AssertionError):
    pass


class People:
    """Stable pseudonyms for people. ``local_dir`` holds the salt and is never committed."""

    def __init__(self, local_dir: Path, bot_accounts: Iterable[str] = ()) -> None:
        self.local = Path(local_dir)
        self.local.mkdir(parents=True, exist_ok=True)
        salt_path = self.local / "people.salt"
        if not salt_path.exists():
            salt_path.write_text(secrets.token_hex(16), encoding="utf-8")
        self.salt = salt_path.read_text(encoding="utf-8").strip()
        self.non_person = GENERIC_BOTS | {b.lower() for b in bot_accounts}
        self.handles: set[str] = set()      # accounts seen as authors (real people)
        self.mentioned: set[str] = set()    # names seen only after "@" in text

    def pseudonym(self, handle: str | None, *, mention: bool = False) -> str | None:
        if not handle:
            return handle
        if handle.lower() in self.non_person or handle.lower().endswith("[bot]"):
            return handle
        if mention:
            if handle not in self.handles:
                self.mentioned.add(handle)
        else:
            self.handles.add(handle)
        digest = hashlib.sha256(f"{self.salt}:{handle.lower()}".encode()).hexdigest()[:8]
        return f"person-{digest}"

    def collect(self, handles: Iterable[str | None]) -> None:
        for h in handles:
            if h and h.lower() not in self.non_person and not h.lower().endswith("[bot]"):
                self.handles.add(h)

    def scrub_text(self, text: str | None) -> str | None:
        if text is None:
            return None
        text = DATA_URI.sub("[embedded image removed]", text)
        text = ENCODED_DATA_URI.sub("[embedded image removed]", text)
        text = SIGNED_PARAM.sub(lambda m: m.group(1) + "[removed]", text)
        text = JWT.sub("[token removed]", text)
        text = SSH_KEY.sub(lambda m: f"{m.group(1)} [public key removed]", text)
        text = re.sub(r"\bAAAAB3NzaC1[A-Za-z0-9+/=]{20,}", "[public key removed]", text)
        text = EMAIL.sub("[email removed]", text)

        def mention(m: re.Match[str]) -> str:
            h = m.group(1)
            if h.lower() in NOT_HANDLES or h.lower() in self.non_person:
                return m.group(0)
            return "@" + (self.pseudonym(h, mention=True) or h)

        text = MENTION.sub(mention, text)
        for h in sorted(self.handles, key=len, reverse=True):
            if len(h) < 4 or h.lower() in NOT_HANDLES:
                continue
            # Bare handles (without @) seen as authors, e.g. "Originally posted by someone".
            text = re.sub(r"(?<![A-Za-z0-9_-])" + re.escape(h) + r"(?![A-Za-z0-9_-])", self.pseudonym(h) or h, text)
        return text

    def scrub(self, value: Any, key: str | None = None) -> Any:
        if isinstance(value, dict):
            return {k: self.scrub(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [self.scrub(v, key) for v in value]
        if isinstance(value, str):
            if key in PERSON_KEYS:
                return self.pseudonym(value)
            return self.scrub_text(value)
        return value

    def write_handles_file(self) -> Path:
        path = self.local / "handles.txt"
        known: set[str] = set()
        if path.exists():
            known = {line.strip() for line in path.read_text(encoding="utf-8").splitlines()
                     if line.strip() and not line.startswith("#")}
        allh = sorted((self.handles | known) - {""}, key=str.lower)
        path.write_text("# Handles seen in raw data; used only by the public check. Never committed.\n"
                        + "\n".join(allh) + "\n", encoding="utf-8")
        return path


def collect_people(db: EvidenceDB, people: People) -> None:
    """Learn every author handle in a copy before scrubbing, so bare mentions of them are caught too."""
    def walk(v: Any, key: str | None = None) -> None:
        if isinstance(v, dict):
            for k, x in v.items():
                walk(x, k)
        elif isinstance(v, list):
            for x in v:
                walk(x, key)
        elif isinstance(v, str) and key in ("author", "editor", "login"):
            people.collect([v])

    for rec in db.all_records():
        walk(db.content(rec.record_id))


def check_temporal_leakage(db: EvidenceDB, cutoff: datetime) -> list[str]:
    """Problems with anything dated after the cut-off. Empty when the copy is clean."""
    problems: list[str] = []
    for rec in db.all_records():
        if rec.valid_from > cutoff:
            problems.append(f"{rec.record_id}: content version after the cutoff")
        content = db.content(rec.record_id) or {}
        if not isinstance(content, dict):
            continue
        for ref in content.get("references", []):
            at = from_iso(ref["at"]) if isinstance(ref, dict) and ref.get("at") else None
            if at and at > cutoff:
                problems.append(f"{rec.record_id}: relationship dated after the cutoff")
        for c in content.get("comments", []):
            if isinstance(c, dict) and c.get("created_at") and from_iso(c["created_at"]) > cutoff:
                problems.append(f"{rec.record_id}: comment after the cutoff")
        for key in ("committed_at", "merged_at", "published"):
            value = content.get(key)
            if isinstance(value, str) and value and from_iso(value) > cutoff:
                problems.append(f"{rec.record_id}: {key.replace('_', ' ')} after the cutoff")
    return problems


def write_export(db: EvidenceDB, target: Path, manifest: dict[str, Any], people: People, *,
                 snapshot_id: str | None = None) -> Path:
    """Write the portable export of a copy, with people scrubbed from every record's content."""
    from atlas.snapshots import normalized_sha256

    target.mkdir(parents=True, exist_ok=True)
    rows = db.export_rows()
    lines = []
    for row in rows:
        row = dict(row)
        if row.get("content"):
            row["content"] = json.dumps(people.scrub(json.loads(row["content"])), sort_keys=True)
        lines.append(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
    records_path = target / "records.jsonl"
    records_path.write_bytes("".join(lines).encode("utf-8"))
    conflicts = db.insufficient_evidence()
    (target / "conflicts.jsonl").write_bytes("".join(json.dumps(c, sort_keys=True) + "\n" for c in conflicts).encode("utf-8"))
    manifest = dict(manifest)
    manifest.update({"snapshot_id": snapshot_id or target.name, "records": len(rows), "records_sha256": normalized_sha256(records_path)})
    (target / "snapshot.json").write_bytes((json.dumps(manifest, indent=1, sort_keys=True, default=str) + "\n").encode("utf-8"))
    return target


def check_export_people(target: Path, people: People) -> list[str]:
    """One pass over the export: tokenise once, then set lookups (no per-handle regex)."""
    problems = []
    text = (target / "records.jsonl").read_text(encoding="utf-8")
    authors = {h.lower() for h in people.handles if len(h) >= 4}
    mentioned = {h.lower() for h in people.mentioned}
    for m in re.finditer(r"(?<![A-Za-z0-9_/.-])(@?)([A-Za-z0-9][A-Za-z0-9-]*)(?![A-Za-z0-9_-])", text):
        token = m.group(2).lower()
        if token in authors:
            problems.append(f"{target.name}: author handle survived pseudonymisation")
            break
        if m.group(1) and token in mentioned:
            problems.append(f"{target.name}: mentioned handle survived pseudonymisation")
            break
    if re.search(r"\b[A-Za-z0-9._%+-]+@(?!users\.noreply\.github\.com)[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", text):
        problems.append(f"{target.name}: email address survived pseudonymisation")
    if re.search(r"eyJ[A-Za-z0-9_-]{8,}\.eyJ", text):
        problems.append(f"{target.name}: signed token survived")
    return problems
