from datetime import timedelta

import pytest

from atlas.contracts import Provenance
from atlas.store import EvidenceDB, ReadOnlyStore, WriterRejected, derived_record, scope_union
from tests.a.helpers import T0, rec


@pytest.fixture
def db(tmp_path):
    d = EvidenceDB(tmp_path / "live.sqlite")
    yield d
    d.close()


def test_authoritative_writer_enforced_and_logged(db):
    bad = rec(writer="connector.git")
    with pytest.raises(WriterRejected):
        db.put(bad, "connector.git")
    assert db.rejected_writes()[0]["writer"] == "connector.git"
    with pytest.raises(WriterRejected):
        db.put(rec(), "someone.else")


def test_older_revision_never_overwrites_newer(db):
    db.put(rec(version="r5", revision=5), "connector.issues")
    db.put(rec(version="r3", revision=3), "connector.issues")
    current = db.get("issues", "issue/1")
    assert current.content_version_id == "r5"
    assert [r.content_version_id for r in db.history("issues", "issue/1")] == ["r3", "r5"]


def test_newer_revision_supersedes_and_history_kept(db):
    db.put(rec(version="r1", revision=1), "connector.issues")
    db.put(rec(version="r2", revision=2), "connector.issues")
    old = db.by_id("issues:issue/1:r1")
    assert old.superseded_by == "issues:issue/1:r2"
    assert db.get("issues", "issue/1").content_version_id == "r2"


def test_duplicate_delivery_is_noop_and_rerun_is_new_record(db):
    first = db.put(rec("ci", "job/42", "attempt1"), "connector.ci", event_key="job42:1:obs-a")
    again = db.put(rec("ci", "job/42", "attempt1-copy", record_id="dup"), "connector.ci", event_key="job42:1:obs-a")
    assert again == first
    assert db.by_id("dup") is None
    rerun = db.put(rec("ci", "job/42", "attempt2"), "connector.ci", event_key="job42:2:obs-a")
    assert rerun != first
    assert len(db.history("ci", "job/42")) == 2


def test_same_content_version_is_noop(db):
    a = db.put(rec(), "connector.issues")
    b = db.put(rec(record_id="other-id"), "connector.issues")
    assert a == b


def test_conflict_creates_node_and_is_attached_on_read(db):
    ticket = rec("issues", "issue/9", "closed")
    commit = rec("git", "pr/9", "unmerged")
    db.put(ticket, "connector.issues")
    db.put(commit, "connector.git")
    node = db.record_conflict("issue 9 fixed", ticket.record_id, commit.record_id)
    got = db.get("issues", "issue/9")
    assert node in got.conflicts and commit.record_id in got.conflicts
    assert db.get("git", "pr/9").conflicts == (node, ticket.record_id)
    assert db.insufficient_evidence()[0]["fact"] == "issue 9 fixed"


def test_force_push_supersedes_evidence_on_orphaned_commits(db):
    db.put(rec("git", "commit/abc", "abc"), "connector.git", commit_sha="abc")
    db.put(rec("ci", "job/1", "a1"), "connector.ci", commit_sha="abc")
    db.put(rec("ci", "job/2", "a1"), "connector.ci", commit_sha="def")
    db.ref_event("refs/heads/main", "def", previous_head="abc", forced=True, orphaned_commits=["abc"], recorded_at=T0)
    assert db.by_id("ci:job/1:a1").superseded_by.startswith("ref-event:")
    assert db.get("ci", "job/1") is None  # no current record any more
    assert db.get("ci", "job/2") is not None


def test_commits_are_identities_not_an_ordering(db):
    db.put(rec("git", "commit/aaa", "aaa", valid_from=T0), "connector.git")
    db.put(rec("git", "commit/bbb", "bbb", valid_from=T0 - timedelta(days=5)), "connector.git")
    assert db.get("git", "commit/aaa") is not None and db.get("git", "commit/bbb") is not None


def test_derived_record_scope_union_and_synthetic_flag(db):
    a = rec(scopes=frozenset({"public"}))
    b = rec("security", "scan/1", "s", scopes=frozenset({"security"}), synthetic=True, provenance=Provenance.SYNTHETIC)
    d = derived_record(record_id="d1", source_id="summary/1", lineage=[a, b], valid_from=T0, content_ref="db:d1")
    assert d.access_scopes == frozenset({"public", "security"}) == scope_union([a, b])
    assert d.synthetic_ancestry
    db.put(d, "graph.builder")
    assert db.get("derived", "summary/1").lineage == (a.record_id, b.record_id)


def test_read_only_file_refuses_writes(tmp_path):
    path = tmp_path / "s.sqlite"
    EvidenceDB(path).close()
    ro = EvidenceDB(path, read_only=True)
    with pytest.raises(ReadOnlyStore):
        ro.put(rec(), "connector.issues")
    ro.close()


def test_search_and_content(db):
    db.put(rec(), "connector.issues", content={"title": "passt uses 100% CPU"}, entity_type="issue")
    found = db.search({"source": "issues", "text": "passt"})
    assert [r.record_id for r in found] == ["issues:issue/1:v1"]
    assert db.content("issues:issue/1:v1")["title"].startswith("passt")
