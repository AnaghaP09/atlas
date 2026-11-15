"""Detection rules for Add project: repository link, labels, tags, CI, owners, dependency list."""

from __future__ import annotations

import tomllib

import pytest

from atlas.contracts import EgressPurpose, RunMode
from atlas.gateway import Gateway, RequestContext, policy_check
from atlas.onboarding import (DETECTED, NEEDS_YOU, OnboardingError, detect, parse_repo, render_block, suggest_labels,
                              tag_pattern)
from tests.onboard.fakes import REPO, FakeGitHub

CTX = RequestContext(purpose=EgressPurpose.LIVE_INGEST, run_mode=RunMode.LIVE)


@pytest.mark.parametrize("text", ["https://github.com/example-org/widget", "github.com/example-org/widget.git",
                                  "example-org/widget", " https://github.com/example-org/widget/issues?q=1 "])
def test_parse_repo_accepts_links_and_names(text):
    assert parse_repo(text) == "example-org/widget"


@pytest.mark.parametrize("text", ["", "widget", "https://gitlab.com/a/b", "a/b/c", "../../etc", "a/..", "-a/b"])
def test_parse_repo_refuses_anything_else(text):
    with pytest.raises(OnboardingError):
        parse_repo(text)


def _labels(*names):
    return [{"name": n} for n in names]


def test_label_rules_pick_the_best_match_and_show_evidence():
    s = suggest_labels(_labels("type: bug", "bug", "bugfix", "not a bug", "kind/flake", "priority/critical-urgent",
                               "regression", "docs"))
    assert s["bug"]["value"] == "bug" and s["bug"]["status"] == DETECTED
    assert "type: bug" in s["bug"]["evidence"]          # other matches are shown
    assert s["flaky"]["value"] == "kind/flake"
    assert s["critical"]["value"] == "priority/critical-urgent"
    assert s["regression"]["value"] == "regression"


@pytest.mark.parametrize("name", ["issue::bug", "kind/bug", "Type: Bug", "type/bug"])
def test_label_rules_bug_variants(name):
    assert suggest_labels(_labels(name, "enhancement"))["bug"]["value"] == name


def test_label_rules_leave_unknowns_to_a_person():
    s = suggest_labels(_labels("bugfix", "not a bug", "possible bug", "enhancement"))
    for key in ("bug", "flaky", "critical", "regression"):
        assert s[key]["value"] == "" and s[key]["status"] == NEEDS_YOU


def test_tag_pattern():
    assert tag_pattern(["v1.2.3", "v1.3.0", "nightly"])[0] == r"^v[0-9]+\.[0-9]+\.[0-9]+$"
    assert tag_pattern(["1.2.3", "2.0.0", "v0.1.0"])[0] == r"^[0-9]+\.[0-9]+\.[0-9]+$"
    assert tag_pattern(["release-a", "nightly"])[0] == ""


def test_detect_proposes_a_block_with_status_and_evidence():
    fake = FakeGitHub()
    gateway = Gateway(fake, tokens={"github": lambda: "t"})  # no project sources: detection still reads
    proposal = detect(gateway, f"https://github.com/{REPO}")
    fields = {f["key"]: f for f in proposal["fields"]}
    assert proposal["repository"] == REPO
    assert fields["project_id"]["value"] == "widget" and fields["project_id"]["status"] == DETECTED
    assert fields["ci"]["value"] == "github_actions" and "2 GitHub Actions workflows" in fields["ci"]["evidence"]
    assert fields["release_tag_pattern"]["value"] == r"^v[0-9]+\.[0-9]+\.[0-9]+$"
    assert fields["latest_release"]["value"] == "v2.0.0"            # the pre-release is not a release
    assert fields["labels.bug"]["value"] == "bug" and "open issues carry it" in fields["labels.bug"]["evidence"]
    assert fields["labels.flaky"]["value"] == "kind/flake"
    assert fields["labels.flaky"]["status"] == NEEDS_YOU            # no open issue carries it now
    assert fields["security_inventory"]["value"] == "uv.lock"
    assert fields["owners"]["value"] == [".github/CODEOWNERS"]
    assert fields["languages"]["value"][0] == "Python"
    assert fields["publish_targets"]["value"] == [] and fields["publish_targets"]["status"] == NEEDS_YOU
    assert all(f["status"] in (DETECTED, NEEDS_YOU) and f["evidence"] for f in proposal["fields"])
    # Only GET reads, counted.
    assert all(m == "GET" for m, _ in fake.calls) and proposal["api_requests"] >= len(fake.calls) - 1
    block = tomllib.loads(proposal["block"])["projects"]["widget"]
    assert block["repository"] == REPO and block["export"]["publish_targets"] == []
    assert block["labels"]["bug"] == "bug"


def test_detect_marks_a_taken_id_for_a_person():
    gateway = Gateway(FakeGitHub(), tokens={"github": lambda: "t"})
    fields = {f["key"]: f for f in detect(gateway, REPO, existing={"widget": object()})["fields"]}
    assert fields["project_id"]["status"] == NEEDS_YOU and fields["project_id"]["value"] == "widget-2"


def test_detect_reports_a_missing_repository():
    gateway = Gateway(lambda m, u, h, b: __import__("atlas.gateway", fromlist=["Response"]).Response(404, {}),
                      tokens={"github": lambda: ""})
    with pytest.raises(OnboardingError) as exc:
        detect(gateway, "example-org/missing")
    assert exc.value.status == 404


@pytest.mark.parametrize("method,url", [
    ("POST", f"https://api.github.com/repos/{REPO}/labels"),
    ("GET", f"https://api.github.com/repos/{REPO}/pulls"),
    ("GET", f"https://api.github.com/repos/{REPO}/git/trees/main"),
    ("GET", f"https://api.github.com/repos/{REPO}/contents/../../x"),
    ("GET", "https://example.com/repos/a/b"),
    ("GET", "https://api.github.com/user"),
])
def test_detection_reads_are_limited_to_repository_facts(method, url):
    assert policy_check("onboarding", method, url, None, CTX) is not None


def test_detection_rows_allow_the_fact_reads():
    for path in ("", "/labels", "/releases", "/tags", "/languages", "/contents", "/contents/.github",
                 "/actions/workflows", "/issues", f"/commits/{'a' * 40}"):
        assert policy_check("onboarding", "GET", f"https://api.github.com/repos/{REPO}{path}", None, CTX) is None


def test_block_never_carries_publish_targets():
    block = render_block({"project_id": "x", "repository": "a/b", "name": "X", "labels": {"bug": 'say "hi"'},
                          "sources": {"go_module": "example.org/x"}})
    data = tomllib.loads(block)["projects"]["x"]
    assert data["export"] == {"publish_targets": [], "notification_audiences": []}
    assert data["labels"]["bug"] == 'say "hi"' and data["sources"]["go_module"] == "example.org/x"
