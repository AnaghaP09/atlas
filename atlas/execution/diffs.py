"""Unified-diff parsing and strict application, without running git or any repository code."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Mapping

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class DiffError(ValueError):
    pass


@dataclass
class Hunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    lines: list[str]  # each starts with ' ', '-', '+'

    def text(self) -> str:
        return f"@@ -{self.old_start},{self.old_len} +{self.new_start},{self.new_len} @@\n" + "".join(
            line if line.endswith("\n") else line + "\n" for line in self.lines
        )


@dataclass
class FileDiff:
    old_path: str | None
    new_path: str | None
    hunks: list[Hunk] = field(default_factory=list)
    binary: bool = False
    mode_change: bool = False

    @property
    def path(self) -> str:
        return (self.new_path or self.old_path or "").strip()

    def text(self) -> str:
        old = f"a/{self.old_path}" if self.old_path else "/dev/null"
        new = f"b/{self.new_path}" if self.new_path else "/dev/null"
        return f"--- {old}\n+++ {new}\n" + "".join(h.text() for h in self.hunks)


def _strip(path: str) -> str | None:
    path = path.split("\t", 1)[0].strip()
    if path == "/dev/null":
        return None
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return path


def _incomplete(h: Hunk) -> bool:
    old = sum(1 for x in h.lines if x[:1] in (" ", "-"))
    new = sum(1 for x in h.lines if x[:1] in (" ", "+"))
    return old < h.old_len or new < h.new_len


def parse(diff: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    current: FileDiff | None = None
    hunk: Hunk | None = None
    lines = diff.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git"):
            current = None
            hunk = None
        elif line.startswith(("GIT binary patch", "Binary files")):
            if current is None:
                current = FileDiff(None, None)
                files.append(current)
            current.binary = True
        elif line.startswith(("old mode", "new mode")):
            if current is not None:
                current.mode_change = True
        elif (line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ ")
              and (hunk is None or not _incomplete(hunk))):
            current = FileDiff(_strip(line[4:]), _strip(lines[i + 1][4:]))
            files.append(current)
            hunk = None
            i += 1
        elif line.startswith("@@"):
            m = HUNK_RE.match(line)
            if not m or current is None:
                raise DiffError(f"malformed hunk header: {line.strip()}")
            hunk = Hunk(int(m.group(1)), int(m.group(2) or 1), int(m.group(3)), int(m.group(4) or 1), [])
            current.hunks.append(hunk)
        elif hunk is not None and line[:1] in (" ", "-", "+"):
            hunk.lines.append(line)
        elif hunk is not None and line.startswith("\\"):
            pass  # "\ No newline at end of file"
        elif line.strip() == "":
            if hunk is not None and _incomplete(hunk):
                hunk.lines.append(" \n" if line.endswith("\n") else " ")
        i += 1
    for f in files:
        for h in f.hunks:
            old = sum(1 for x in h.lines if x[:1] in (" ", "-"))
            new = sum(1 for x in h.lines if x[:1] in (" ", "+"))
            if (old, new) != (h.old_len, h.new_len):
                raise DiffError(f"{f.path}: hunk line counts do not match its header")
    return files


def apply_file(original: str | None, fd: FileDiff) -> str | None:
    """Apply one file diff strictly; every context and removed line must match exactly."""
    if fd.binary:
        raise DiffError(f"{fd.path}: binary changes are not applied")
    if fd.new_path is None:
        return None
    src = (original or "").splitlines(keepends=True)
    out: list[str] = []
    pos = 0
    for h in fd.hunks:
        start = max(h.old_start - 1, 0) if h.old_len else h.old_start
        if start < pos:
            raise DiffError(f"{fd.path}: overlapping hunks")
        out.extend(src[pos:start])
        pos = start
        for line in h.lines:
            tag, body = line[:1], line[1:]
            if tag in (" ", "-"):
                if pos >= len(src) or src[pos].rstrip("\r\n") != body.rstrip("\r\n"):
                    raise DiffError(f"{fd.path}: context does not match at line {pos + 1}")
                if tag == " ":
                    out.append(src[pos])
                pos += 1
            elif tag == "+":
                out.append(body if body.endswith("\n") else body + "\n")
    out.extend(src[pos:])
    return "".join(out)


def apply(tree: Mapping[str, str], files: list[FileDiff]) -> dict[str, str]:
    result = dict(tree)
    for fd in files:
        if fd.old_path and fd.old_path not in result and fd.hunks and any(l[:1] in (" ", "-") for h in fd.hunks for l in h.lines):
            raise DiffError(f"{fd.old_path}: file not present at the base commit")
        new = apply_file(result.get(fd.old_path) if fd.old_path else None, fd)
        if fd.old_path and fd.old_path != fd.new_path:
            result.pop(fd.old_path, None)
        if new is None:
            result.pop(fd.path, None)
        else:
            result[fd.new_path] = new  # type: ignore[index]
    return result


def tree_sha256(tree: Mapping[str, str]) -> str:
    h = hashlib.sha256()
    for path in sorted(tree):
        h.update(path.encode())
        h.update(b"\0")
        h.update(hashlib.sha256(tree[path].encode()).hexdigest().encode())
        h.update(b"\n")
    return h.hexdigest()


def render(files: list[FileDiff]) -> str:
    return "".join(f.text() for f in files)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)$")


def recount(diff_text: str) -> str:
    """Rewrite each hunk header so its line counts match the hunk body.

    Coding agents often miscount hunk lengths. The body is left untouched, and applying the diff
    still checks every context and removed line against the base file.
    """
    lines = diff_text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        m = _HUNK_HEADER.match(lines[i])
        if not m:
            out.append(lines[i])
            i += 1
            continue
        j = i + 1
        old = new = 0
        while j < len(lines) and not lines[j].startswith(("@@", "--- ", "+++ ", "diff ")):
            line = lines[j]
            if line.startswith("\\"):
                pass
            elif line.startswith("+"):
                new += 1
            elif line.startswith("-"):
                old += 1
            else:
                old += 1
                new += 1
            j += 1
        out.append(f"@@ -{m.group(1)},{old} +{m.group(2)},{new} @@{m.group(3)}")
        out.extend(lines[i + 1:j])
        i = j
    newline = chr(10)
    return newline.join(out) + (newline if diff_text.endswith(newline) else "")
