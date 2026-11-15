"""Add project API: audit role only, Idempotency-Key, atomic projects file, live watching, no publish grant."""

from __future__ import annotations

import os
import tomllib
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from atlas.api.app import create_app
from atlas.api.identity import HeaderIdentity
from atlas.api.services import build_core
from atlas.contracts import Mode
from atlas.data_wiring import connect_data
from atlas.feed import ProjectFeed
from atlas.projects import ProjectRegistry
from tests.d.conftest import BASE
from tests.onboard.fakes import REPO, TAG, FakeGitHub

EXISTING = """# Projects
[projects.first]
name = "First"
repository = "example-org/first"
ci = "github_actions"

[projects.first.export]
publish_targets = ["example-org/sandbox"]
notification_audiences = []
"""


def H(role: str, key: str | None = None) -> dict[str, str]:
    return {"X-Atlas-Role": role, "Idempotency-Key": key or str(uuid.uuid4())}


@pytest.fixture
def env(tmp_path):
    projects_file = tmp_path / "local" / "projects.toml"
    projects_file.parent.mkdir(parents=True)
    projects_file.write_text(EXISTING, encoding="utf-8")
    registry = ProjectRegistry.load(projects_file)
    s = build_core(identity=HeaderIdentity(projects=registry, default_project="first"))
    s.projects = registry
    s.projects_file = projects_file
    s.default_project_id = "first"
    s.add_project_prefill = f"https://github.com/{REPO}"
    s.feed = ProjectFeed(s.hub)
    fake = FakeGitHub()
    settings = SimpleNamespace(data_dir=tmp_path, github_token="t", publish_token="")
    data = connect_data(settings, {"ATLAS_PROJECTS_FILE": str(projects_file), "ATLAS_DASHBOARD_POLL_SECONDS": "60"},
                        mode=lambda: Mode.CONNECTED, feed=s.feed, transport=fake, target_dir=tmp_path / "target")
    s.wiring = SimpleNamespace(extra={"data": data}, clock=None)
    return SimpleNamespace(services=s, client=TestClient(create_app(s), base_url=BASE), file=projects_file, data=data,
                           fake=fake)


def _project(**over):
    body = {"id": "widget", "name": "Widget", "repository": REPO, "ci": "github_actions",
            "release_tag_pattern": r"^v[0-9]+\.[0-9]+\.[0-9]+$", "issue_query": 'is:issue label:"bug"',
            "security_inventory": "uv.lock", "labels": {"bug": "bug", "flaky": "", "critical": "", "regression": ""}}
    body.update(over)
    return {"project": body}


@pytest.mark.parametrize("role", ["pm", "em", "architect", "security", "oncall"])
def test_only_the_audit_role_adds_projects(env, role):
    c = env.client
    assert c.get("/api/projects/add/defaults", headers=H(role)).status_code == 403
    assert c.post("/api/projects/detect", json={"repository": REPO}, headers=H(role)).status_code == 403
    assert c.post("/api/projects", json=_project(), headers=H(role)).status_code == 403
    assert c.post("/api/projects/first/data-copies?project_id=first", json={"tag": TAG}, headers=H(role)).status_code == 403
    assert env.file.read_text(encoding="utf-8") == EXISTING


def test_defaults_carry_the_prefilled_link(env):
    body = env.client.get("/api/projects/add/defaults", headers=H("audit")).json()
    assert body["repository"] == f"https://github.com/{REPO}" and body["connected"] and body["github_token"]


def test_detect_needs_an_idempotency_key_and_replays(env):
    c = env.client
    r = c.post("/api/projects/detect", json={"repository": REPO}, headers={"X-Atlas-Role": "audit"})
    assert r.status_code == 400
    key = str(uuid.uuid4())
    first = c.post("/api/projects/detect", json={"repository": REPO}, headers=H("audit", key))
    assert first.status_code == 200 and first.json()["project"]["id"] == "widget"
    calls = len(env.fake.calls)
    again = c.post("/api/projects/detect", json={"repository": REPO}, headers=H("audit", key))
    assert again.headers.get("Idempotent-Replay") == "true" and len(env.fake.calls) == calls
    other = c.post("/api/projects/detect", json={"repository": "example-org/other"}, headers=H("audit", key))
    assert other.status_code == 409


def test_add_writes_the_file_starts_watching_and_grants_nothing(env):
    c, s = env.client, env.services
    key = str(uuid.uuid4())
    r = c.post("/api/projects", json=_project(), headers=H("audit", key))
    assert r.status_code == 201, r.text
    assert r.json()["id"] == "widget" and r.json()["publish_targets"] == []
    data = tomllib.loads(env.file.read_text(encoding="utf-8"))["projects"]
    assert data["first"]["export"]["publish_targets"] == ["example-org/sandbox"]      # the old block is untouched
    assert data["widget"]["repository"] == REPO
    assert data["widget"]["export"] == {"publish_targets": [], "notification_audiences": []}
    assert not s.projects.may_publish("widget", "example-org/sandbox")
    assert not s.projects.may_publish("widget", REPO)
    assert REPO in env.data.gateway.sources.repositories()                          # read-only rows
    assert "widget" in env.data.pollers and "dashboard:widget" in s.scheduler.watchers
    assert s.scheduler.watchers["release:widget"].default_enabled is False           # spending runs stay paused
    listed = c.get("/api/projects", headers=H("audit")).json()["projects"]
    assert {p["id"] for p in listed} == {"first", "widget"}
    board = c.get("/api/projects/widget/dashboard", headers=H("pm")).json()
    assert board["project_id"] == "widget"
    # A repeat with the same key replays; a new key is refused as a duplicate.
    assert c.post("/api/projects", json=_project(), headers=H("audit", key)).headers.get("Idempotent-Replay") == "true"
    dup = c.post("/api/projects", json=_project(), headers=H("audit"))
    assert dup.status_code == 409
    same_repo = c.post("/api/projects", json=_project(id="widget-two"), headers=H("audit"))
    assert same_repo.status_code == 409 and "already watched" in same_repo.json()["reason"]
    assert any(e["action"] == "project.add" for e in s.audit.entries("widget"))


@pytest.mark.parametrize("over", [
    {"export": {"publish_targets": ["example-org/sandbox"]}},
    {"publish_targets": ["example-org/sandbox"]},
    {"id": "Bad Id"},
    {"repository": "not a repo"},
    {"ci": "jenkins"},
    {"release_tag_pattern": "(unclosed"},
    {"labels": {"owner": "x"}},
    {"name": "two\nlines"},
    {"security_inventory": "../secrets.txt"},
])
def test_add_refuses_bad_blocks_and_any_publish_grant(env, over):
    r = env.client.post("/api/projects", json=_project(**over), headers=H("audit"))
    assert r.status_code == 400, r.text
    assert env.file.read_text(encoding="utf-8") == EXISTING


def test_projects_file_is_replaced_in_one_step(env, monkeypatch):
    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        env.client.post("/api/projects", json=_project(), headers=H("audit"))
    assert env.file.read_text(encoding="utf-8") == EXISTING
    assert [p.name for p in env.file.parent.iterdir() if p.name.startswith(".projects-")] == []
    assert "widget" not in env.services.projects.ids()


def test_copies_api_builds_and_lists_progress(env):
    c = env.client
    assert c.post("/api/projects", json=_project(), headers=H("audit")).status_code == 201
    tags = c.get("/api/projects/widget/release-tags?project_id=widget", headers=H("audit")).json()["tags"]
    assert [t["tag"] for t in tags] == [TAG] and not tags[0]["copy_exists"]
    env.services.data_copies = None
    from atlas.api import routes_onboarding

    builder = routes_onboarding._builder(env.services)
    builder.run_in_background = lambda fn: fn()
    builder.authenticated = lambda: False  # the fake answers the REST reads used without a token
    r = c.post("/api/projects/widget/data-copies?project_id=widget", json={"tag": TAG}, headers=H("audit"))
    assert r.status_code == 202, r.text
    jobs = c.get("/api/projects/widget/data-copies?project_id=widget", headers=H("audit")).json()["jobs"]
    assert jobs[0]["state"] == "done", jobs[0]["message"]
    assert jobs[0]["title"].startswith("Widget at release v2.0.0 · 1 Mar 2026 · ")
    snaps = env.data.snapshots.list(project_id="widget")
    assert [s["snapshot_id"] for s in snaps] == ["widget.release-close-v2.0.0"]
    # Another project's jobs and copies do not show this one.
    assert c.get("/api/projects/first/data-copies", headers=H("audit")).json()["jobs"] == []
    assert env.data.snapshots.list(project_id="first") == []
