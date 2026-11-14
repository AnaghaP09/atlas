"""Non-executing checks on a prepared diff: scope, binaries, protected paths, secrets and personal data.

None of these run repository code. Each returns ``passed`` or ``failed`` with reasons.
"""

from __future__ import annotations

import fnmatch
import math
import re
from dataclasses import dataclass
from typing import Iterable, Sequence

from atlas.execution.diffs import FileDiff

PROTECTED_PATTERNS = (
    ".github/*", ".github/**", ".atlas/*", ".atlas/**", "**/.github/**",
    "go.mod", "go.sum", "Dockerfile", "**/Dockerfile", "Makefile", "*.yml", "*.yaml", "**/*.yml", "**/*.yaml",
    ".gitattributes", ".gitmodules", ".git/**",
)

SECRET_PATTERNS = (
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)(password|passwd|secret|api[_-]?key|token)\s*[:=]\s*['\"][^'\"]{8,}['\"]"),
    re.compile(r"(?i)authorization:\s*bearer\s+[A-Za-z0-9._\-]{16,}"),
)
EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
HIGH_ENTROPY = re.compile(r"[A-Za-z0-9+/_\-]{32,}={0,2}")


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str  # passed | failed | unavailable
    reasons: tuple[str, ...] = ()


def _matches(path: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatch(path, p) for p in patterns)


def scope_check(files: Sequence[FileDiff], scope: Sequence[str]) -> CheckResult:
    bad = [f.path for f in files if not _matches(f.path, scope) or (f.old_path and not _matches(f.old_path, scope))]
    return CheckResult("scope", "failed" if bad else "passed", tuple(f"outside permitted scope: {p}" for p in bad))


def binary_check(files: Sequence[FileDiff]) -> CheckResult:
    bad = [f.path or "(unknown)" for f in files if f.binary or f.mode_change]
    return CheckResult("binary", "failed" if bad else "passed", tuple(f"binary or mode change: {p}" for p in bad))


def config_path_check(files: Sequence[FileDiff]) -> CheckResult:
    bad = [f.path for f in files if _matches(f.path, PROTECTED_PATTERNS) or (f.old_path and _matches(f.old_path, PROTECTED_PATTERNS))]
    return CheckResult("config_paths", "failed" if bad else "passed",
                       tuple(f"CI, policy, release or dependency configuration: {p}" for p in bad))


def _entropy(token: str) -> float:
    counts = {c: token.count(c) for c in set(token)}
    return -sum((n / len(token)) * math.log2(n / len(token)) for n in counts.values())


def secret_findings(text: str) -> list[str]:
    found = [p.pattern[:40] for p in SECRET_PATTERNS if p.search(text)]
    for token in HIGH_ENTROPY.findall(text):
        if _entropy(token) >= 4.5 and not re.fullmatch(r"[0-9a-f]+", token):
            found.append("high-entropy string")
            break
    return found


def added_text(files: Sequence[FileDiff]) -> str:
    return "".join(line[1:] for f in files for h in f.hunks for line in h.lines if line.startswith("+"))


def secret_scan(text: str) -> CheckResult:
    found = secret_findings(text)
    return CheckResult("secret_scan", "failed" if found else "passed", tuple(f"possible secret: {f}" for f in found))


def personal_data_scan(text: str, allowed: Iterable[str] = ("users.noreply.github.com",)) -> CheckResult:
    emails = [e for e in EMAIL.findall(text) if not any(e.endswith(a) for a in allowed)]
    return CheckResult("personal_data_scan", "failed" if emails else "passed",
                       tuple("email address present" for _ in emails[:1]))


def static_checks(files: Sequence[FileDiff], scope: Sequence[str], extra_text: str = "") -> list[CheckResult]:
    text = added_text(files) + "\n" + extra_text
    return [scope_check(files, scope), binary_check(files), config_path_check(files), secret_scan(text),
            personal_data_scan(text)]
