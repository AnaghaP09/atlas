"""Stable pseudonyms for people: names, handles and emails never reach a committed file.

The salt and the handle inventory live only in ``data/local`` (never committed). The handle list
is also written to ``data/local/handles.txt`` so the repository's public check can catch any
handle that slips into a committed file.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from pathlib import Path
from typing import Any, Iterable

from prep.reference import paths, reference

LOCAL = paths()["local"]
MENTION = re.compile(r"(?<![\w/.-])@([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))(?![\w-])")
DATA_URI = re.compile(r"data:[A-Za-z0-9.+/-]+;base64,[A-Za-z0-9+/=]+")
SSH_KEY = re.compile(r"\b(ssh-(?:rsa|ed25519|dss)|ecdsa-sha2-nistp\d+) AAAA[A-Za-z0-9+/=]{20,}")
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
# Bots and organisation accounts are not people; keep them so the evidence stays readable.
GENERIC_BOTS = {"dependabot", "dependabot[bot]", "github-actions", "github-actions[bot]", "copilot"}
NON_PERSON = GENERIC_BOTS | {b.lower() for b in reference().project.bot_accounts}
# Words that look like mentions in technical text but are not accounts.
NOT_HANDLES = {"example", "types", "param", "return", "todo", "v1", "v2", "latest", "main", "host"}


class People:
    def __init__(self, local_dir: Path = LOCAL) -> None:
        self.local = local_dir
        self.local.mkdir(parents=True, exist_ok=True)
        salt_path = self.local / "people.salt"
        if not salt_path.exists():
            salt_path.write_text(secrets.token_hex(16), encoding="utf-8")
        self.salt = salt_path.read_text(encoding="utf-8").strip()
        self.handles: set[str] = set()

    def pseudonym(self, handle: str | None) -> str | None:
        if not handle:
            return handle
        if handle.lower() in NON_PERSON or handle.lower().endswith("[bot]"):
            return handle
        self.handles.add(handle)
        digest = hashlib.sha256(f"{self.salt}:{handle.lower()}".encode()).hexdigest()[:8]
        return f"person-{digest}"

    def collect(self, handles: Iterable[str | None]) -> None:
        for h in handles:
            if h and h.lower() not in NON_PERSON and not h.lower().endswith("[bot]"):
                self.handles.add(h)

    def scrub_text(self, text: str | None) -> str | None:
        if text is None:
            return None
        text = DATA_URI.sub("[embedded image removed]", text)
        text = SSH_KEY.sub(lambda m: f"{m.group(1)} [public key removed]", text)
        text = re.sub(r"\bAAAAB3NzaC1[A-Za-z0-9+/=]{20,}", "[public key removed]", text)
        text = EMAIL.sub("[email removed]", text)

        def mention(m: re.Match[str]) -> str:
            h = m.group(1)
            if h.lower() in NOT_HANDLES or h.lower() in NON_PERSON:
                return m.group(0)
            return "@" + (self.pseudonym(h) or h)

        text = MENTION.sub(mention, text)
        for h in sorted(self.handles, key=len, reverse=True):
            if len(h) < 4 or h.lower() in NOT_HANDLES:
                continue
            # Bare handles (without @) seen as authors, e.g. "Originally posted by someone".
            text = re.sub(r"(?<![\w-])" + re.escape(h) + r"(?![\w-])", self.pseudonym(h) or h, text)
        return text

    def scrub(self, value: Any, key: str | None = None) -> Any:
        if isinstance(value, dict):
            return {k: self.scrub(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [self.scrub(v, key) for v in value]
        if isinstance(value, str):
            if key in ("author", "editor", "login", "actor", "assignee"):
                return self.pseudonym(value)
            return self.scrub_text(value)
        return value

    def write_handles_file(self) -> Path:
        path = self.local / "handles.txt"
        existing = set()
        if path.exists():
            existing = {h.strip() for h in path.read_text(encoding="utf-8").splitlines() if h.strip() and not h.startswith("#")}
        allh = sorted((existing | self.handles) - {""}, key=str.lower)
        path.write_text("# Handles seen in raw data; used only by the public check. Never committed.\n" + "\n".join(allh) + "\n", encoding="utf-8")
        return path
