"""Check that nothing unsuitable for a public repository is committed.

Scans tracked files (and, with --history, every commit message) for:
  - secrets: known token formats, private keys and long high-entropy strings;
  - personal data: email addresses (except example domains and GitHub no-reply addresses),
    and any handle listed in data/local/handles.txt when that local file exists;
  - internal project markers that belong in private planning notes, not in the product.

Exit code 0 means clean; 1 means at least one hit (each printed as path:line: reason).
Usage: python scripts/public_check.py [--history] [--staged] [paths...]
"""

from __future__ import annotations

import argparse
import math
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Patterns are assembled from parts so this file does not match itself.
SECRET_PATTERNS = [
    ("anthropic key", re.compile("sk-" + r"ant-[A-Za-z0-9_\-]{20,}")),
    ("github token", re.compile(r"\b(?:gh[pousr]_" + r"[A-Za-z0-9]{30,}|github_" + r"pat_[A-Za-z0-9_]{30,})")),
    ("aws key", re.compile(r"\bAK" + r"IA[0-9A-Z]{16}\b")),
    ("slack token", re.compile(r"\bxox" + r"[baprs]-[A-Za-z0-9-]{10,}")),
    ("private key", re.compile("-----BEGIN [A-Z ]*PRIV" + "ATE KEY-----")),
]

EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
ALLOWED_EMAIL_DOMAINS = {"example.com", "example.org", "example.net", "users.noreply.github.com"}

INTERNAL_MARKERS = [
    ("document version label", re.compile(r"\bDR" + r"AFT v\d")),
    ("planning document name", re.compile(r"\bPL" + r"AN-[A-E]\b")),
    ("workstream label", re.compile(r"\bW-" + r"[A-E]\b")),
    ("review round label", re.compile(r"\bro" + r"und [0-9]\b", re.IGNORECASE)),
    ("review finding id", re.compile(r"\b[A-Z]{1,2}[0-9]?-?R?[0-9]+-[0-9]{2}\b")),
    ("planning term", re.compile(r"\bsub" + r"-plan\b", re.IGNORECASE)),
    ("review tool name", re.compile(r"\bCo" + r"dex\b")),
]

ENTROPY_TOKEN = re.compile(r"[A-Za-z0-9+/_\-]{32,}")
ENTROPY_THRESHOLD = 4.5

# Lock files carry long hashes by design; they are checked for token formats only.
ENTROPY_EXEMPT = {"requirements.txt", "requirements-dev.txt", "package-lock.json"}
BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz", ".sqlite", ".woff", ".woff2"}


def shannon_entropy(text: str) -> float:
    counts = Counter(text)
    total = len(text)
    return -sum(c / total * math.log2(c / total) for c in counts.values())


def load_handles() -> list[str]:
    path = ROOT / "data" / "local" / "handles.txt"
    if not path.exists():
        return []
    return [h.strip() for h in path.read_text(encoding="utf-8").splitlines() if h.strip() and not h.startswith("#")]


def scan_text(text: str, label: str, *, entropy: bool = True, handles: list[str] | None = None) -> list[str]:
    hits: list[str] = []
    handle_patterns = [(h, re.compile(r"(?<![\w-])@?" + re.escape(h) + r"(?![\w-])")) for h in (handles or [])]
    for number, line in enumerate(text.splitlines(), 1):
        where = f"{label}:{number}"
        for name, pattern in SECRET_PATTERNS:
            if pattern.search(line):
                hits.append(f"{where}: possible {name}")
        for match in EMAIL.finditer(line):
            if match.group(1).lower() not in ALLOWED_EMAIL_DOMAINS:
                hits.append(f"{where}: email address")
        for name, pattern in INTERNAL_MARKERS:
            if pattern.search(line):
                hits.append(f"{where}: internal marker ({name})")
        for handle, pattern in handle_patterns:
            if pattern.search(line):
                hits.append(f"{where}: personal handle")
        if entropy:
            for token in ENTROPY_TOKEN.findall(line):
                if shannon_entropy(token) >= ENTROPY_THRESHOLD:
                    hits.append(f"{where}: high-entropy string")
                    break
    return hits


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def tracked_files(staged: bool) -> list[Path]:
    names = git("diff", "--cached", "--name-only", "--diff-filter=ACM").splitlines() if staged else git("ls-files").splitlines()
    return [ROOT / n for n in names if n]


def scan_paths(paths: list[Path]) -> list[str]:
    handles = load_handles()
    hits: list[str] = []
    for path in paths:
        if not path.is_file() or path.suffix.lower() in BINARY_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        rel = path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else str(path)
        hits += scan_text(text, rel, entropy=path.name not in ENTROPY_EXEMPT, handles=handles)
    return hits


def scan_history() -> list[str]:
    hits: list[str] = []
    log = git("log", "--format=%H%x00%B%x1e")
    for entry in log.split("\x1e"):
        entry = entry.strip()
        if not entry:
            continue
        sha, _, message = entry.partition("\x00")
        hits += scan_text(message, f"commit {sha[:10]}", handles=load_handles())
    return hits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--history", action="store_true", help="also scan every commit message")
    parser.add_argument("--staged", action="store_true", help="scan staged files instead of tracked files")
    parser.add_argument("paths", nargs="*", type=Path)
    args = parser.parse_args(argv)

    paths = [p.resolve() for p in args.paths] if args.paths else tracked_files(args.staged)
    hits = scan_paths(paths)
    if args.history:
        hits += scan_history()
    for hit in hits:
        print(hit)
    print(f"public check: {len(hits)} issue(s)", file=sys.stderr)
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
