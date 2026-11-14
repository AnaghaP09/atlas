from datetime import datetime, timedelta, timezone

from atlas.connectors.normalize import TITLE_UNAVAILABLE, issue_as_of, pull_request_as_of, to_record

CUT = datetime(2026, 6, 24, 9, 0, tzinfo=timezone.utc)


def iso(t):
    return t.isoformat().replace("+00:00", "Z")


def issue(**over):
    created = CUT - timedelta(hours=2)
    base = {
        "number": 100,
        "title": "Flaky: network test fails (root cause: passt bug)",
        "body": "Edited later: root cause is the passt bug, fixed in #200",
        "createdAt": iso(created),
        "closedAt": iso(CUT + timedelta(days=5)),
        "state": "CLOSED",
        "url": "https://github.com/kubevirt/kubevirt/issues/100",
        "author": {"login": "reporter1"},
        "labels": {"nodes": [{"name": "kind/flake"}, {"name": "triage/accepted"}]},
        "userContentEdits": {"nodes": [
            {"editedAt": iso(CUT + timedelta(days=1)), "diff": "Edited later: root cause is the passt bug, fixed in #200"},
            {"editedAt": iso(created), "diff": "Network test flakes since the image update."},
        ]},
        "edits_complete": True,
        "timeline": [
            {"__typename": "LabeledEvent", "createdAt": iso(created), "label": {"name": "kind/flake"}},
            {"__typename": "LabeledEvent", "createdAt": iso(CUT + timedelta(days=2)), "label": {"name": "triage/accepted"}},
            {"__typename": "RenamedTitleEvent", "createdAt": iso(CUT + timedelta(days=1)), "previousTitle": "Flaky: network test fails", "currentTitle": "Flaky: network test fails (root cause: passt bug)"},
            {"__typename": "CrossReferencedEvent", "createdAt": iso(CUT - timedelta(minutes=30)), "willCloseTarget": False, "source": {"__typename": "Issue", "number": 99}},
            {"__typename": "CrossReferencedEvent", "createdAt": iso(CUT + timedelta(days=3)), "willCloseTarget": True, "source": {"__typename": "PullRequest", "number": 200}},
            {"__typename": "ClosedEvent", "createdAt": iso(CUT + timedelta(days=5)), "stateReason": "COMPLETED", "closer": {"__typename": "PullRequest", "number": 200}},
        ],
        "timeline_complete": True,
        "comments_list": [
            {"databaseId": 1, "createdAt": iso(CUT - timedelta(minutes=10)), "body": "edited: it is the passt bug", "author": {"login": "dev2"},
             "userContentEdits": {"nodes": [{"editedAt": iso(CUT + timedelta(days=1)), "diff": "edited: it is the passt bug"},
                                            {"editedAt": iso(CUT - timedelta(minutes=10)), "diff": "seeing this too"}]}},
            {"databaseId": 2, "createdAt": iso(CUT + timedelta(hours=1)), "body": "/close fixed by #200", "author": {"login": "dev3"}, "userContentEdits": {"nodes": []}},
        ],
        "comments_complete": True,
    }
    base.update(over)
    return base


def test_issue_closed_later_with_fix_shows_only_pre_cutoff_versions():
    v = issue_as_of(issue(), CUT).view
    assert v["body"] == "Network test flakes since the image update."
    assert v["title"] == "Flaky: network test fails"
    assert v["state"] == "OPEN" and v["closed_by"] is None
    assert v["labels"] == ["kind/flake"]
    assert [r["number"] for r in v["references"]] == [99]
    assert [c["body"] for c in v["comments"]] == ["seeing this too"]
    blob = str(v)
    assert "#200" not in blob and "passt bug" not in blob and "root cause" not in blob


def test_issue_created_after_cutoff_is_invisible():
    assert issue_as_of(issue(createdAt=iso(CUT + timedelta(seconds=1))), CUT) is None


def test_incomplete_history_excludes_fields():
    v = issue_as_of(issue(timeline_complete=False), CUT)
    assert v.view["title"] == TITLE_UNAVAILABLE
    assert v.view["labels"] == [] and v.view["state"] == "UNKNOWN" and v.view["references"] == []
    assert {"title", "labels", "state", "references"} <= set(v.missing)
    assert not v.history_complete


def test_body_unavailable_when_edit_list_incomplete_and_all_later():
    raw = issue(userContentEdits={"nodes": [{"editedAt": iso(CUT + timedelta(days=1)), "diff": "later text"}]}, edits_complete=False)
    v = issue_as_of(raw, CUT)
    assert v.view["body"] is None and "body" in v.missing


def test_unedited_body_is_current_body():
    raw = issue(body="original only", userContentEdits={"nodes": []})
    assert issue_as_of(raw, CUT).view["body"] == "original only"


def test_reopen_after_close_before_cutoff():
    t = CUT - timedelta(hours=1)
    raw = issue(timeline=[
        {"__typename": "ClosedEvent", "createdAt": iso(t), "stateReason": "COMPLETED", "closer": None},
        {"__typename": "ReopenedEvent", "createdAt": iso(t + timedelta(minutes=5))},
    ])
    assert issue_as_of(raw, CUT).view["state"] == "OPEN"


def pr(**over):
    base = {
        "number": 300,
        "title": "Fix: revert the verification block that breaks upgrades",
        "body": "This fixes the upgrade flake reported in #18465",
        "createdAt": iso(CUT - timedelta(days=10)),
        "mergedAt": iso(CUT - timedelta(days=8)),
        "baseRefName": "main",
        "author": {"login": "dev4"},
        "mergeCommit": {"oid": "9e58ac0fa2" + "0" * 30},
        "files": {"totalCount": 1, "nodes": [{"path": "tests/upgrade_test.go"}]},
        "userContentEdits": {"nodes": [
            {"editedAt": iso(CUT + timedelta(days=20)), "diff": "This fixes the upgrade flake reported in #18465"},
            {"editedAt": iso(CUT - timedelta(days=10)), "diff": "Add a verification block to the upgrade test"},
        ]},
        "timelineItems": {"nodes": [
            {"__typename": "RenamedTitleEvent", "createdAt": iso(CUT + timedelta(days=20)), "previousTitle": "Add verification block to upgrade test", "currentTitle": "Fix: revert the verification block that breaks upgrades"},
        ]},
    }
    base.update(over)
    return base


def test_pr_merged_before_cutoff_but_edited_after_shows_old_versions():
    v = pull_request_as_of(pr(), CUT).view
    assert v["title"] == "Add verification block to upgrade test"
    assert v["body"] == "Add a verification block to the upgrade test"
    assert "18465" not in str(v)


def test_pr_merged_after_cutoff_is_invisible():
    assert pull_request_as_of(pr(mergedAt=iso(CUT + timedelta(minutes=1))), CUT) is None
    assert pull_request_as_of(pr(mergedAt=None), CUT) is None


def test_record_confirmation_by_basis():
    asof = issue_as_of(issue(), CUT)
    fetched = CUT + timedelta(days=100)
    rec, extra = to_record("issues", asof, cutoff=CUT, fetched_at=fetched, history_complete_through=fetched)
    assert rec.confirmed_through == CUT and extra["freshness_basis"] == "reconstructed"
    assert rec.valid_from <= CUT and rec.fetched_at == fetched
    incomplete = issue_as_of(issue(comments_complete=False), CUT)
    rec2, _ = to_record("issues", incomplete, cutoff=CUT, fetched_at=fetched, history_complete_through=fetched)
    assert rec2.confirmed_through is None
    live, extra3 = to_record("issues", asof, cutoff=CUT, fetched_at=CUT, history_complete_through=None, live=True)
    assert live.confirmed_through == CUT and extra3["freshness_basis"] == "live"
