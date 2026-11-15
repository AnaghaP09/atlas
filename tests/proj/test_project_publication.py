"""Publication, corrections and setup stay inside the task's project."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from atlas.execution import artefact as artefacts
from atlas.execution.corrections import CorrectionError
from atlas.execution.publication import PublicationRefused
from atlas.projection import AuthorizationProjection
from deploy.init import grant_publish_target
from tests.c.fakes import REPO, make_env

EXAMPLE = Path(__file__).resolve().parents[2] / "deploy" / "projects.example.toml"


def _set_project(env, task_id: str, project_id: str) -> None:
    with env.tasks.db.transaction() as conn:
        conn.execute("UPDATE tasks SET project_id = ? WHERE task_id = ?", (project_id, task_id))


# ---------------------------------------------------------------- (1) setup fills the first grant

def test_setup_fills_the_first_projects_empty_publish_targets(tmp_path):
    projects = tmp_path / "projects.toml"
    projects.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    assert grant_publish_target(projects, "kubevirt", "owner/test-target") is True
    data = tomllib.loads(projects.read_text(encoding="utf-8"))["projects"]
    assert data["kubevirt"]["export"]["publish_targets"] == ["owner/test-target"]
    assert data["argo-cd"]["export"]["publish_targets"] == []
    # An owner-edited list is never overwritten; placeholders and empty values are ignored.
    assert grant_publish_target(projects, "kubevirt", "owner/other") is False
    assert grant_publish_target(projects, "argo-cd", "<owner>/<repo>") is False
    assert grant_publish_target(projects, "argo-cd", "") is False
    assert grant_publish_target(projects, "argo-cd", "owner/argo-target") is True
    assert tomllib.loads(projects.read_text(encoding="utf-8"))["projects"]["argo-cd"]["export"]["publish_targets"] == ["owner/argo-target"]


def test_setup_never_grants_a_target_another_project_already_lists(tmp_path):
    projects = tmp_path / "projects.toml"
    projects.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    assert grant_publish_target(projects, "argo-cd", "owner/shared") is True
    assert grant_publish_target(projects, "kubevirt", "owner/shared") is False
    assert grant_publish_target(projects, "kubevirt", "Owner/Shared") is False
    data = tomllib.loads(projects.read_text(encoding="utf-8"))["projects"]
    assert data["kubevirt"]["export"]["publish_targets"] == []


def test_setup_grants_only_on_its_first_run_and_says_what_it_did(tmp_path):
    from deploy.init import first_run_publish_grant

    projects = tmp_path / "projects.toml"
    projects.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    assert first_run_publish_grant(projects, "argo-cd", "", first_run=True) is None
    said = first_run_publish_grant(projects, "argo-cd", "owner/target", first_run=False)
    assert "Left publish targets as they are" in said
    assert all(p["export"]["publish_targets"] == []
               for p in tomllib.loads(projects.read_text(encoding="utf-8"))["projects"].values())
    said = first_run_publish_grant(projects, "argo-cd", "owner/target", first_run=True)
    assert "Let project 'argo-cd' publish to owner/target" in said
    # A second run, even one that thinks it is first, never hands the same target to another project.
    said = first_run_publish_grant(projects, "kubevirt", "owner/target", first_run=True)
    assert "already publishes to owner/target" in said
    data = tomllib.loads(projects.read_text(encoding="utf-8"))["projects"]
    assert data["kubevirt"]["export"]["publish_targets"] == []
    assert "not an owner/repository name" in first_run_publish_grant(projects, "kubevirt", "<owner>/<repo>",
                                                                     first_run=True)


def test_setup_rerun_through_run_keeps_publish_targets(tmp_path, monkeypatch):
    from deploy import init

    (tmp_path / ".env.example").write_text("ATLAS_PUBLISH_TARGET=owner/target\n", encoding="utf-8")
    monkeypatch.setattr(init, "find_openssl", lambda _=None: None)  # stop after the project step
    import io
    first, second = io.StringIO(), io.StringIO()
    init.run([], out=first, repo_root=tmp_path)
    init.run([], out=second, repo_root=tmp_path)
    data = tomllib.loads((tmp_path / "data" / "local" / "projects.toml").read_text(encoding="utf-8"))["projects"]
    granted = [pid for pid, p in data.items() if "owner/target" in p["export"]["publish_targets"]]
    assert granted == ["argo-cd"]
    assert "Let project 'argo-cd' publish to owner/target" in first.getvalue()
    assert "already publishes to owner/target" in second.getvalue()


def test_the_example_names_no_repository_to_publish_to():
    data = tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))["projects"]
    assert all(p["export"]["publish_targets"] == [] for p in data.values())
    assert "ATLAS_PUBLISH_TARGET" in EXAMPLE.read_text(encoding="utf-8")


# ---------------------------------------------------------------- (2) grant at every publication step

def test_publish_grant_is_checked_before_every_step():
    env = make_env()
    task = env.to_approved_b()
    _set_project(env, task.task_id, "kubevirt")
    seen = []

    def grant(project_id, target):
        seen.append((project_id, target))
        return True

    env.publisher.publish_grant = grant
    env.publisher.publish(task.task_id, env.now)
    assert seen == [("kubevirt", REPO)] * 3  # push, pull request, description


def test_a_withdrawn_grant_stops_publication_mid_way():
    env = make_env()
    task = env.to_approved_b()
    _set_project(env, task.task_id, "kubevirt")
    calls = {"n": 0}

    def grant(project_id, target):
        calls["n"] += 1
        return calls["n"] == 1  # allowed for the push only

    env.publisher.publish_grant = grant
    with pytest.raises(PublicationRefused) as err:
        env.publisher.publish(task.task_id, env.now)
    assert "no export grant" in str(err.value)
    steps = {r["step"]: r["status"] for r in env.publisher.journal(env.tasks.get(task.task_id).action_id)}
    assert steps == {"push": "done"}


# ---------------------------------------------------------------- (3) project-bound public principal

def test_publication_keeps_its_own_projects_items_and_withholds_others():
    projection = AuthorizationProjection()
    changes = [
        artefacts.SourceChange("own.go", "own.go", [{"text": "+a\n", "required_scopes": ["public"], "project_id": "kubevirt"}], ["public"]),
        artefacts.SourceChange("other.go", "other.go", [{"text": "+b\n", "required_scopes": ["public"], "project_id": "argo-cd"}], ["public"]),
        artefacts.SourceChange("plain.go", "plain.go", [{"text": "+c\n", "required_scopes": ["public"]}], ["public"]),
    ]
    out = artefacts._project(projection, changes, "msg", "title", "body", {}, "kubevirt")
    kept = {f["path"]: len(f["hunks"]) for f in out["files"] if f}
    assert kept == {"own.go": 1, "other.go": 0, "plain.go": 1}
    legacy = artefacts._project(projection, changes, "msg", "title", "body", {}, None)
    assert {f["path"]: len(f["hunks"]) for f in legacy["files"] if f}["own.go"] == 0
    assert artefacts.public_principal("kubevirt").is_public and artefacts.public_principal("kubevirt").project_id == "kubevirt"


# ---------------------------------------------------------------- (4) corrections carry the project

def _published_incident(env, project_id="kubevirt"):
    task = env.to_published()
    _set_project(env, task.task_id, project_id)
    pub = f"pr:{REPO}#{env.tasks.get(task.task_id).pr_number}"
    inc = env.containment.trip(8, producer="adjudication", detail={}, now=env.now, publication_id=pub,
                               offending_item_id="claim-3")
    return pub, inc


def test_corrections_store_the_publications_project_and_refuse_other_projects():
    env = make_env()
    pub, inc = _published_incident(env)
    assert env.corrections.publication_project(pub) == "kubevirt"
    with pytest.raises(CorrectionError):
        env.corrections.preauthorisation_read(inc, role="audit", project_id="argo-cd")
    read = env.corrections.preauthorisation_read(inc, role="audit", project_id="kubevirt")
    with pytest.raises(CorrectionError):
        env.corrections.authorise(inc, replacement_text="Corrected text.", expected_body_sha256=read["body_sha256"],
                                  role="audit", now=env.now, project_id="argo-cd")
    assert env.tasks.db.query("SELECT * FROM correction_authorisations") == []
    auth = env.corrections.authorise(inc, replacement_text="Corrected text.", expected_body_sha256=read["body_sha256"],
                                     role="audit", now=env.now, project_id="kubevirt")
    row = env.tasks.db.one("SELECT project_id FROM correction_authorisations WHERE authorisation_id = ?", (auth,))
    assert row["project_id"] == "kubevirt"
    with pytest.raises(CorrectionError):
        env.corrections.execute(auth, now=env.now, project_id="argo-cd")
    assert env.tasks.db.query("SELECT * FROM corrections") == []
    result = env.corrections.execute(auth, now=env.now, project_id="kubevirt")
    assert result["state"] == "done"
    stored = env.tasks.db.one("SELECT project_id FROM corrections WHERE correction_id = ?", (result["correction_id"],))
    assert stored["project_id"] == "kubevirt"
    assert [c["project_id"] for c in env.corrections.list(inc, project_id="kubevirt")] == ["kubevirt"]
    assert env.corrections.list(inc, project_id="argo-cd") == []
