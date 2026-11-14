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


@pytest.mark.parametrize(
    "text",
    [
        j("Built by ", "Swe", "tha"),
        j("thanks to ", "Bhunesh", "wari"),
        j("owner: ", "Ana", "gha"),
        j("for the ", "ju", "ry"),
        j("two ", "Ju", "rors"),
    ],
)
def test_names_and_review_words_detected(text: str) -> None:
    assert public_check.scan_text(text, "f", entropy=False)


def test_account_handle_is_not_a_name() -> None:
    assert not public_check.scan_text(j("https://github.com/", "Ana", "ghaP09/atlas"), "f", entropy=False)


@pytest.mark.parametrize(
    "text",
    [
        j("path = 'C:", "\\\\", "Us", "ers", "\\\\", "someone", "\\\\", "project'"),
        j("C:/", "Us", "ers/someone/tools/bin"),
        j("cache=/", "ho", "me/someone/.cache"),
        j("cd /", "Us", "ers/someone/work"),
        j("%LOCAL", "APPDATA% or C:/x/", "App", "Data/Local/Programs"),
    ],
)
def test_machine_specific_values_detected(text: str) -> None:
    assert public_check.scan_text(text, "f", entropy=False)


def test_relative_and_example_paths_allowed() -> None:
    for text in ("ATLAS_DATA_DIR=data", "data/local/images", "https://atlas.invalid/schema/x.json",
                 "ATLAS_PUBLISH_TARGET=<owner>/<repo>"):
        assert not public_check.scan_text(text, "f", entropy=False)


def test_handles_match_in_one_pass_and_ignore_case() -> None:
    handles = frozenset(f"user-{i}" for i in range(400)) | {"Octo-Dev"}
    assert public_check.scan_text("thanks octo-dev!", "f", handles=handles)
    assert not public_check.scan_text("octo-developer and pre-octo-dev", "f", handles=handles)


def test_data_files_skip_entropy_and_label_checks_but_keep_personal_checks() -> None:
    token = "".join(chr(65 + (i * 7) % 26) + chr(97 + (i * 11) % 26) + str(i % 10) for i in range(14))
    url_line = "https://storage.example.com/logs/" + token + "/build.log # " + "round" + " 1"
    assert public_check.scan_text(url_line, "src/module.py")
    assert not public_check.scan_text(url_line, "data/snapshots/x.jsonl", data=True)
    assert public_check.is_data_path("data/snapshots/case/records.jsonl")
    assert public_check.is_data_path("data/evaluator/expected.csv")
    assert not public_check.is_data_path("atlas/store.py")
    # Secrets, emails and handles are still blocked in data files.
    assert public_check.scan_text("contact someone" + "@" + "corp-mail.org", "d", data=True)
    assert public_check.scan_text("token ghp_" + "a" * 36, "d", data=True)
    assert public_check.scan_text("fixed by @octo-dev", "d", data=True, handles=["octo-dev"])


def test_numbers_are_not_matched_as_handles() -> None:
    assert not public_check.scan_text("see pull request #30787", "d", data=True, handles=["30787"])
    assert public_check.scan_text("thanks user30787", "d", data=True, handles=["user30787"])
