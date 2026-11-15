"""The on-demand "Data as of" copy builder: as-of rules, cleaning, leakage and registration."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from atlas.cleaning import People, check_temporal_leakage, write_export, check_export_people
from atlas.connectors.normalize import issue_as_of
from atlas.connectors.project import ProjectSources, SourceSet
from atlas.contracts import Provenance, Record, RunContext, RunMode
from atlas.datacopy import CopyBuilder, copy_id, copy_title, rest_issue_raw
from atlas.gateway import Gateway
from atlas.snapshots import SnapshotRegistry
from atlas.store import AUTHORITATIVE_WRITERS, EvidenceDB, from_iso
from tests.onboard.fakes import AFTER, CUTOFF, ISSUES, REPO, TAG, TIMELINES, FakeGitHub, _issue

T = from_iso(CUTOFF)


def _builder(tmp_path, fake, *, token=False, labels=None):
    sources = SourceSet((ProjectSources.from_mapping("widget", {"repository": REPO}),))
    gateway = Gateway(fake, tokens={"github": lambda: "t" if token else ""}, sources=sources,
                      clock=lambda: datetime(2026, 11, 1, tzinfo=timezone.utc))
    registry = SnapshotRegistry(tmp_path, sources=sources)
    cfg = {"repository": REPO, "labels": labels if labels is not None else {"bug": "bug"},
           "security_inventory": "uv.lock", "name": "Widget"}
    builder = CopyBuilder(data_dir=tmp_path, gateway=gateway, registry=registry, project=lambda pid: cfg,
                          authenticated=lambda: token, run_in_background=lambda fn: fn(),
                          clock=lambda: datetime(2026, 11, 1, tzinfo=timezone.utc))
    return builder, registry


def test_rest_history_rebuilds_the_issue_as_of_the_tag():
    raw = rest_issue_raw(ISSUES[0], TIMELINES[1], True, T)
    view = issue_as_of(raw, T).view
    assert view["state"] == "OPEN" and view["labels"] == ["bug"]           # "wontfix" came later
    assert [c["body"] for c in view["comments"]] == ["Same here, cc @bob-example"]  # the later comment is left out
    closed_later = issue_as_of(rest_issue_raw(ISSUES[1], TIMELINES[2], True, T), T).view
    assert closed_later["state"] == "OPEN"


def test_rest_history_drops_text_that_may_have_changed_after_the_tag():
    issue = _issue(9, "2026-01-01T00:00:00Z", updated=AFTER)
    edited = [{"event": "commented", "created_at": "2026-01-02T00:00:00Z", "updated_at": AFTER, "body": "edited later",
               "user": {"login": "x-example"}, "id": 1}]
    view = issue_as_of(rest_issue_raw(issue, edited, True, T), T).view
    assert view["body"] is None and "body" in view["missing"]
    assert view["comments"][0]["body"] is None and view["comments"][0]["body_missing"]


def test_incomplete_history_gives_unknown_state():
    view = issue_as_of(rest_issue_raw(ISSUES[0], TIMELINES[1], False, T), T).view
    assert view["state"] == "UNKNOWN"


def test_copy_without_token_for_a_small_repository(tmp_path):
    fake = FakeGitHub()
    builder, registry = _builder(tmp_path, fake)
    job = builder.start("widget", TAG)
    assert job.state == "done", job.message
    sid = copy_id("widget", TAG)
    assert job.snapshot_id == sid
    assert job.title == copy_title("Widget", TAG, T, 2) == "Widget at release v2.0.0 · 1 Mar 2026 · 2 open issues"
    listed = {s["snapshot_id"]: s for s in registry.list_snapshots("widget")}
    assert listed[sid]["kind"] == "releases" and listed[sid]["title"] == job.title
    assert registry.list_snapshots("another") == []                 # registered with its project only
    assert job.requests <= job.budget
    ctx = RunContext(run_id="r", snapshot_id=sid, mode=RunMode.REPLAY, release_or_case=TAG, cutoff=T,
                     alias_variant=None, evidence_clock=T, project_id="widget")
    db = registry.open(ctx)
    issues = {db.content(r.record_id)["number"]: db.content(r.record_id) for r in db.search({"source": "issues"})}
    assert set(issues) == {1, 2}                                    # #3 came later, #4 was closed before
    assert issues[1]["labels"] == ["bug"] and issues[2]["state"] == "OPEN"
    assert all(r.valid_from <= T for r in db.all_records())
    # Dependency scan as of the tag: the advisory published later is left out.
    findings = [db.content(r.record_id) for r in db.search({"source": "security", "limit": 100})]
    run = next(f for f in findings if f.get("kind") == "scan_run")
    assert run["advisories_excluded_after_cutoff"] == 1 and "PyPI" in run["scanner"]
    assert [f["advisory_ids"] for f in findings if f.get("kind") == "finding"] == [["OSV-OLD-1"]]
    # Components from folders, with the team from CODEOWNERS.
    comps = {db.content(r.record_id)["component"]: db.content(r.record_id)
             for r in db.search({"source": "architecture", "limit": 100}) if "component" in db.content(r.record_id)}
    assert "src/widget/web" in comps and comps["src/widget/web"]["owner_team"] == "example-org/web"
    # People are pseudonyms; nobody's handle survives in the export.
    text = (tmp_path / "local" / "snapshots" / "releases" / sid / "records.jsonl").read_text(encoding="utf-8")
    for handle in ("alice-example", "bob-example", "carol-example", "erin-example"):
        assert handle not in text
    assert "person-" in text
    manifest = json.loads((tmp_path / "local" / "snapshots" / "releases" / sid / "snapshot.json").read_text(encoding="utf-8"))
    assert manifest["project_id"] == "widget" and manifest["cutoff"].startswith("2026-03-01")
    # Only reads left the machine.
    assert all(m == "GET" or path == "/v1/querybatch" for m, path in fake.calls)


def test_copy_without_token_refuses_a_large_repository(tmp_path):
    many = [_issue(n, "2026-01-01T00:00:00Z") for n in range(1, 101)]
    builder, registry = _builder(tmp_path, FakeGitHub(issues=many))
    job = builder.start("widget", TAG)
    assert job.state == "refused" and "GITHUB_TOKEN" in job.message and "60 requests an hour" in job.message
    assert registry.list_snapshots("widget") == []


def test_copy_stops_at_the_request_budget(tmp_path, monkeypatch):
    import atlas.datacopy as dc

    monkeypatch.setattr(dc, "NO_TOKEN_REQUESTS", 3)
    builder, registry = _builder(tmp_path, FakeGitHub())
    job = builder.start("widget", TAG)
    assert job.state == "failed" and "budget" in job.message and job.requests == 3
    assert registry.list_snapshots("widget") == []


def test_copy_refuses_an_unknown_tag_and_a_second_job(tmp_path):
    builder, _ = _builder(tmp_path, FakeGitHub())
    assert builder.start("widget", "v9.9.9").state == "refused"
    from atlas.datacopy import CopyJob, CopyRefused

    builder.jobs["busy"] = CopyJob(job_id="busy", project_id="widget", tag=TAG, state="running")
    with pytest.raises(CopyRefused):
        builder.start("widget", TAG)
    with pytest.raises(CopyRefused):
        builder.start("widget", "../etc")


def _rec(source_id: str, at: datetime) -> Record:
    return Record(record_id=f"issues:{source_id}@v", source="issues", source_id=source_id, revision=None,
                  content_version_id="v", valid_from=at, observed_at=at, confirmed_through=at,
                  writer=AUTHORITATIVE_WRITERS["issues"], provenance=Provenance.OBSERVED, synthetic_ancestry=False,
                  access_scopes=frozenset({"public"}), lineage=(), content_ref=f"db:issues:{source_id}@v")


def test_leakage_rules_catch_anything_after_the_cutoff(tmp_path):
    db = EvidenceDB(tmp_path / "x.sqlite")
    db.put(_rec("issue/1", T), AUTHORITATIVE_WRITERS["issues"], content={"comments": [{"created_at": AFTER}]})
    db.put(_rec("issue/2", from_iso(AFTER)), AUTHORITATIVE_WRITERS["issues"], content={})
    db.put(_rec("issue/3", T), AUTHORITATIVE_WRITERS["issues"], content={"references": [{"at": AFTER}]})
    problems = check_temporal_leakage(db, T)
    assert any("comment after" in p for p in problems)
    assert any("content version after" in p for p in problems)
    assert any("relationship dated after" in p for p in problems)


def test_cleaning_scrubs_people_and_secrets(tmp_path):
    db = EvidenceDB(tmp_path / "y.sqlite")
    db.put(_rec("issue/1", T), AUTHORITATIVE_WRITERS["issues"],
           content={"author": "frank-example", "body": "ping @grace-example, mail me at frank@example.com, "
                                                       "https://x.test/a?token=abc123 ; dependabot[bot] agrees"})
    people = People(tmp_path / "local")
    from atlas.cleaning import collect_people

    collect_people(db, people)
    target = write_export(db, tmp_path / "out" / "copy-1", {"cutoff": CUTOFF}, people)
    text = (target / "records.jsonl").read_text(encoding="utf-8")
    assert "frank-example" not in text and "grace-example" not in text and "frank@example.com" not in text
    assert "abc123" not in text and "dependabot[bot]" in text
    assert check_export_people(target, people) == []
