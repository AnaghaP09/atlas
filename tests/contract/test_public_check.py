"""The public-repository check finds secrets, personal data and internal markers."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("public_check", ROOT / "scripts" / "public_check.py")
public_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(public_check)


def j(*parts: str) -> str:
    """Build test strings from parts so this file itself stays clean."""
    return "".join(parts)


@pytest.mark.parametrize(
    "text",
    [
        j("key = ", "sk-", "ant-", "a" * 30),
        j("token ", "gh", "p_", "A1b2" * 9),
        j("AK", "IA", "ABCDEFGHIJKLMNOP"),
        j("-----BEGIN RSA PRIV", "ATE KEY-----"),
    ],
)
def test_secret_formats_detected(text: str) -> None:
    assert public_check.scan_text(text, "f")


def test_high_entropy_detected_but_lock_hashes_exempt(tmp_path: Path) -> None:
    token = j("Zx9Qw3Er7Ty1Ui5O", "p2As6Df0Gh4Jk8Lm")
    assert public_check.scan_text(f"value={token}", "f")
    assert not public_check.scan_text(f"value={token}", "f", entropy=False)


def test_email_detected_except_allowed_domains() -> None:
    assert public_check.scan_text(j("contact: someone", "@", "gmail.com"), "f")
    assert not public_check.scan_text(j("owner", "@", "example.com"), "f")
    assert not public_check.scan_text(j("bot", "@", "users.noreply.github.com"), "f")


@pytest.mark.parametrize(
    "text",
    [
        j("DR", "AFT v3"),
        j("see PL", "AN-B"),
        j("owned by W", "-C"),
        j("fixed in ro", "und 4"),
        j("finding C-R", "4-02"),
        j("the sub", "-plan says"),
        j("reviewed by Co", "dex"),
    ],
)
def test_internal_markers_detected(text: str) -> None:
    assert any("internal marker" in hit for hit in public_check.scan_text(text, "f"))


@pytest.mark.parametrize(
    "text",
    [
        "Add evidence store with revision ordering",
        "Released on 2026-11-14 at 10:00",
        "Uses SHA-256 hashes and UTF-8 text",
        "Gate G-13 checks forecast validity",
    ],
)
def test_ordinary_text_is_clean(text: str) -> None:
    assert not public_check.scan_text(text, "f")


def test_handles_from_local_inventory() -> None:
    assert public_check.scan_text("reported by @octo-dev today", "f", handles=["octo-dev"])
    assert not public_check.scan_text("reported by @someone-else", "f", handles=["octo-dev"])


def test_repository_is_clean() -> None:
    import subprocess

    names = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    hits = public_check.scan_paths([ROOT / n for n in names if n])
    assert not hits, hits
