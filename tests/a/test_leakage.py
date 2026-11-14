"""The leakage checks catch planted outcome material, and committed snapshots pass them."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from atlas.store import EvidenceDB
from prep.snapshot.build import check_leakage
from prep.snapshot.people import People
from prep.snapshot.releases import check_close_leakage
from tests.a.helpers import rec

CUT = datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    d = EvidenceDB(tmp_path / "case.sqlite")
    yield d
    d.close()


def test_clean_case_passes(db):
    db.put(rec(valid_from=CUT - timedelta(hours=1)), "connector.issues", content={"body": "flaky upgrade test", "references": [], "comments": []})
    assert check_leakage(db, CUT, {18718}) == []


def test_content_after_cutoff_is_caught(db):
    db.put(rec(valid_from=CUT + timedelta(seconds=1)), "connector.issues", content={"body": "x"})
    assert any("after the cutoff" in p for p in check_leakage(db, CUT, set()))


def test_mention_of_fix_pr_is_caught(db):
    db.put(rec(valid_from=CUT - timedelta(hours=1)), "connector.issues", content={"body": "fixed by #18718"})
    assert any("prohibited pull request" in p for p in check_leakage(db, CUT, {18718}))


def test_fix_pr_record_is_caught(db):
    db.put(rec("git", "pr/18718", "v1", valid_from=CUT - timedelta(days=1)), "connector.git", content={"number": 18718})
    assert any("prohibited fix pull request" in p for p in check_leakage(db, CUT, {18718}))


def test_later_relationship_or_comment_is_caught(db):
    db.put(rec(valid_from=CUT - timedelta(hours=1)), "connector.issues", content={
        "references": [{"number": 1, "at": (CUT + timedelta(days=1)).isoformat()}],
        "comments": [{"created_at": (CUT + timedelta(minutes=5)).isoformat(), "body": "later"}],
    })
    problems = check_leakage(db, CUT, set())
    assert any("relationship dated after" in p for p in problems) and any("comment after" in p for p in problems)


def test_release_close_leakage_catches_later_backport(db):
    db.put(rec("git", "pr/19000", "v1", valid_from=CUT - timedelta(days=1)), "connector.git", content={"number": 19000})
    db.put(rec(valid_from=CUT - timedelta(days=2)), "connector.issues", content={"references": [{"number": 19000}]})
    problems = check_close_leakage(db, CUT, {19000})
    assert any("later back-port present" in p for p in problems) and any("relationship to a later back-port" in p for p in problems)


def test_pseudonymisation_removes_handles_emails_images_and_keys(tmp_path):
    people = People(tmp_path)
    people.collect(["somedev", "kubevirt-bot"])
    email = "dev.person" + "@" + "corp.example.org"
    blob = "PHN2ZyB3aWR0aD0i" + "ODYiIGhlaWdodD0i" + "ODkiPjwvc3ZnPg=="
    key = "AAAAB3NzaC1yc2EA" + "AAADAQABAAABgQC7" + "example0000000000"
    text = (f"@somedev saw it; ping @otherdev. Mail me at {email}. "
            f"![x](data:image/svg+xml;base64,{blob}) "
            f"key: ssh-rsa {key} user. Originally posted by somedev")
    out = people.scrub_text(text)
    assert "somedev" not in out and "otherdev" not in out and "@person-" in out
    assert "corp.example.org" not in out and "base64" not in out and "AAAAB3" not in out
    assert people.scrub({"author": "somedev"})["author"].startswith("person-")
    assert people.scrub({"author": "kubevirt-bot"})["author"] == "kubevirt-bot"
    signed = "https://img.example/a.png?jwt=" + "eyJhbGciOiJIUzI1NiJ9" + ".eyJpc3MiOiJ4In0" + ".sigsigsigsig&w=1"
    badge = "logo=data%3Aimage%2Fsvg%2Bxml%3Bbase64%2C" + "PHN2ZyB3aWR0aD0i" + "ODYi"
    cleaned = people.scrub_text(signed + " " + badge)
    assert "eyJ" not in cleaned and "base64" not in cleaned
    # Handles directly followed by non-ASCII text are still replaced.
    assert "somedev" not in people.scrub_text("cc @somedev我们 and somedev说")


COMMITTED = Path("data") / "snapshots"


@pytest.mark.parametrize("export", sorted(p for kind in ("cases", "releases") for p in (COMMITTED / kind).glob("*") if (p / "snapshot.json").exists()) or [None])
def test_committed_snapshots_are_before_their_cutoff(export):
    if export is None:
        pytest.skip("no committed case snapshots yet")
    manifest = json.loads((export / "snapshot.json").read_text(encoding="utf-8"))
    cutoff = datetime.fromisoformat(manifest["cutoff"])
    for line in (export / "records.jsonl").read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        assert datetime.fromisoformat(row["valid_from"]) <= cutoff, row["record_id"]
