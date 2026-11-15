"""The publisher, the target CI reader and validation, end to end through the real wiring.

The remote repository is a stateful fake (or a local bare git repository); the gateway, the action
store, the task store, the approvals, the eligibility checks and the API are the real ones.
"""

from __future__ import annotations

import subprocess
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from atlas.api.app import create_app
from atlas.api.services import build_core
from atlas.config import load_settings
from atlas.contracts import ModelResult, RunContext, RunMode, StartedBy, StartedByKind, Role, ValidationContext
from atlas.execution.publication import git_blob_id
from atlas.gateway import Response
from atlas.wiring import ValidationEvidenceMemory, connect
from tests.c.fakes import FIX_DIFF, GitBackedPublicationAPI, LostResponse, FakePublicationAPI, PassingSandbox, REPO, TARGET

BASE = "http://127.0.0.1:8000"
PROJECTS = f"""
[projects.alpha]
repository = "example/alpha"
[projects.alpha.export]
publish_targets = ["{REPO}"]
"""


def H(role: str) -> dict[str, str]:
    return {"X-Atlas-Role": role, "Idempotency-Key": str(uuid.uuid4()), "Origin": BASE}


class Provider:
    name = "recorded"

    def complete(self, role, messages, schema, limits):
        content = {"coding_agent": {"diff": FIX_DIFF},
                   "verifier": {"passed": True, "reason": "the diff wraps IPv6 hosts in brackets as asked"}}.get(role)
        if content is None:
            return ModelResult({}, "failed", 0, 0, "recorded", "recorded")
        return ModelResult(content, "complete", 10, 10, "recorded", "recorded")


def base_files() -> dict[str, str]:
    return {p.relative_to(TARGET).as_posix(): p.read_text(encoding="utf-8")
            for p in TARGET.rglob("*") if p.is_file()}


class Remote(FakePublicationAPI):
    """The stateful fake remote, plus the base branch content used for the base comparison."""

    def __init__(self) -> None:
        super().__init__()
        self.base = {path: git_blob_id(text) for path, text in base_files().items()}
        self.base["LICENSE"] = git_blob_id("licence text kept by the target\n")
        self.base["README.md"] = git_blob_id("the target's own readme\n")

    def base_blobs(self, full_name: str, base_ref: str) -> dict[str, str]:
        self.calls.append("base_blobs")
        return dict(self.base)


class TargetCI:
    """Answers the gateway's target CI reads for one repository; everything else is unreachable."""

    def __init__(self) -> None:
        self.jobs: list[dict] = []
        self.requests: list[str] = []

    def __call__(self, method, url, headers, body):
        self.requests.append(f"{method} {url}")
        if f"/repos/{REPO}/actions/runs?" in url:
            return Response(200, {"workflow_runs": [{"id": 77, "run_attempt": 1}]}, {})
        if url.endswith(f"/repos/{REPO}/actions/runs/77/jobs"):
            return Response(200, {"jobs": list(self.jobs)}, {})
        if "/check-runs" in url:
            return Response(200, {"check_runs": []}, {})
        return Response(503, None, {})


class Server:
    def __init__(self, tmp_path, remote) -> None:
        (tmp_path / "local").mkdir(parents=True, exist_ok=True)
        (tmp_path / "local" / "projects.toml").write_text(PROJECTS, encoding="utf-8")
        env = {"ATLAS_DATA_DIR": str(tmp_path), "ATLAS_DEFAULT_PROJECT": "alpha", "ATLAS_PUBLISH_TARGET": REPO,
               "ATLAS_SANDBOX_RUNNER": "none"}
        self.settings = load_settings(None, environ=env)
        self.services = build_core()
        self.services.default_project_id = "alpha"
        from atlas.projects import ProjectRegistry

        self.services.projects = ProjectRegistry.load(tmp_path / "local" / "projects.toml")
        self.ci = TargetCI()
        self.remote = remote
        self.wiring = connect(self.services, self.settings, env=env, provider=Provider(), runner=PassingSandbox(),
                              data_transport=self.ci, publication_api=remote)
        self.services.model_configured = True
        self.client = TestClient(create_app(self.services), base_url=BASE)

    def delegated_run(self) -> str:
        snapshot_id = self.client.post("/api/snapshots", headers=H("em"), json={}).json()["snapshot_id"]
        adm = self.services.admissions.admit(f"test:{uuid.uuid4().hex}", {"trigger": "failure", "event_id": "e1"},
                                             snapshot_id, StartedBy(kind=StartedByKind.PERSON, role=Role.ONCALL),
                                             project_id="alpha")
        now = datetime.now(timezone.utc)
        ctx = RunContext(run_id=adm.run_id, snapshot_id=snapshot_id, mode=RunMode.REPLAY, release_or_case="example",
                         cutoff=now, alias_variant=None, evidence_clock=now, project_id="alpha")
        runs = self.wiring.orchestrator.runs
        runs.save_run(adm.run_id, ctx, {"trigger": "failure", "event_id": "e1"}, {"kind": "person", "role": "oncall"}, {})
        runs.write_generation(adm.run_id, {
            "run_id": adm.run_id, "project_id": "alpha",
            "action": {"value": "DELEGATE_TO_CODING_AGENT", "claim_ids": ["c1"]},
            "classification": {"value": "TEST_DEFECT", "claim_ids": ["c1"]},
            "root_cause": {"text": "The IPv6 tests were not adapted; JoinHostPort does not bracket IPv6 hosts.",
                           "claim_ids": ["c1"]},
            "claims": [{"id": "c1", "text": "only the IPv6 lane fails", "verdict": "supported"}],
            "injection_detected": False})
        return adm.run_id

    def approve(self, task_id: str, kind: str):
        row = next(a for a in self.client.get("/api/approvals", headers=H("em")).json()["approvals"]
                   if a["task_id"] == task_id and a["kind"] == kind and a["state"] == "requested")
        return self.client.post(f"/api/approvals/{row['approval_id']}/decision", headers=H("em"),
                                json={"decision": "approve", "review_minutes": 2})

    def to_approved_b(self) -> str:
        created = self.client.post("/api/tasks", headers=H("em"), json={"run_id": self.delegated_run()})
        assert created.status_code == 201, created.text
        task_id = created.json()["task"]["task_id"]
        assert self.approve(task_id, "A").status_code == 200
        prepared = self.client.post(f"/api/tasks/{task_id}/prepare", headers=H("em"), json={})
        assert prepared.status_code in (200, 201), prepared.text
        assert self.approve(task_id, "B").status_code == 200
        return task_id

    def state(self, task_id: str) -> str:
        return self.client.get(f"/api/tasks/{task_id}", headers=H("em")).json()["task"]["state"]


@pytest.fixture()
def server(tmp_path):
    return Server(tmp_path, Remote())


def test_task_binds_the_real_destination_identity(server):
    task_id = server.to_approved_b()
    task = server.wiring.tasks.get(task_id)
    assert task.destination_node_id == server.remote.identities[REPO].node_id != "unresolved"


def test_published_task_is_validated_accepted_and_scored(server):
    task_id = server.to_approved_b()
    out = server.client.post(f"/api/tasks/{task_id}/publish", headers=H("em"), json={})
    assert out.status_code in (200, 201), out.text
    task = server.wiring.tasks.get(task_id)
    assert task.state.value == "published" and task.pr_number == 1
    journal = server.wiring.publisher.journal(task.action_id)
    assert [r["step"] for r in journal if r["status"] == "done"] == ["push", "pull_request", "description"]
    pr = server.remote.pulls[REPO][0]
    assert pr.draft and pr.head == task.publication_ref and pr.body == task.artefact["body"]

    # Validation evidence is in the action store, not in memory.
    assert not isinstance(server.wiring.validation.evidence, ValidationEvidenceMemory)
    rows = {s["schedule_id"]: s for s in server.client.get("/api/schedules", headers=H("em")).json()["schedules"]}
    assert rows["target-ci"]["state"] == "enabled" and rows["target-ci"]["spends_model_budget"] is False

    # CI still running: the task stays published.
    server.ci.jobs = [{"id": 5, "run_attempt": 1, "name": "test", "status": "in_progress", "conclusion": None,
                       "started_at": "2026-11-15T10:00:00Z", "completed_at": None, "head_sha": task.published_commit}]
    assert server.client.post("/api/schedules/target-ci/run-once", headers=H("em"), json={}).status_code == 200
    assert server.state(task_id) == "published"

    # CI passed: the behavioural check (run by that job) is recorded and the task waits for a person.
    server.ci.jobs = [dict(server.ci.jobs[0], status="completed", conclusion="success",
                           completed_at="2026-11-15T10:03:00Z")]
    server.client.post("/api/schedules/target-ci/run-once", headers=H("em"), json={})
    assert server.state(task_id) == "awaiting_validation"
    ctx = ValidationContext(REPO, task.action_id, task.published_commit)
    store = server.wiring.extra["data"].action_store
    assert len(store.ci_observations(ctx, "execution")) == 2
    assert [e["content"]["passed"] for e in store.evidence(ctx, "execution", "behavioural_check")] == [True]
    assert store.has_lifecycle_state() is False
    assert all(r.startswith("GET ") and f"/repos/{REPO}/" in r for r in server.ci.requests)

    # A person confirms the fix: accepted, and the scorecard has the row.
    judged = server.client.post(f"/api/tasks/{task_id}/adjudicate", headers=H("architect"),
                                json={"verdict": "correct", "note": "IPv6 hosts are bracketed"})
    assert judged.status_code in (200, 201), judged.text
    assert server.state(task_id) == "accepted"
    assert [e["content"]["correct"] for e in store.evidence(ctx, "execution", "human_adjudication")] == [True]
    rows = server.client.get("/api/scorecard", headers=H("em")).json()["rows"]
    assert len(rows) == 1 and rows[0]["task_id"] == task_id
    assert rows[0]["completed"] is True and rows[0]["ci"] == "passed" and rows[0]["behavioural"] == "passed"
    assert rows[0]["not_measured"] == ["review_minutes", "cost_usd"]

    # Monitoring continues after acceptance; a later repeat of the same result changes nothing.
    server.client.post("/api/schedules/target-ci/run-once", headers=H("em"), json={})
    assert server.state(task_id) == "accepted" and len(store.ci_observations(ctx, "execution")) == 2


def test_failed_ci_fails_the_task(server):
    task_id = server.to_approved_b()
    server.client.post(f"/api/tasks/{task_id}/publish", headers=H("em"), json={})
    task = server.wiring.tasks.get(task_id)
    server.ci.jobs = [{"id": 9, "run_attempt": 1, "name": "test", "status": "completed", "conclusion": "failure",
                       "started_at": "2026-11-15T10:00:00Z", "completed_at": "2026-11-15T10:02:00Z",
                       "head_sha": task.published_commit}]
    server.wiring.monitor.poll_all()
    assert server.state(task_id) == "failed"
    row = server.client.get("/api/scorecard", headers=H("em")).json()["rows"][0]
    assert row["completed"] is False and row["ci"] == "failed" and row["behavioural"] == "failed"


@pytest.mark.parametrize("step", ["push", "create_pull", "replace_body"])
def test_lost_response_after_each_remote_effect_is_reconciled_through_the_wiring(server, step):
    task_id = server.to_approved_b()
    server.remote.lose_after.add(step)
    first = server.client.post(f"/api/tasks/{task_id}/publish", headers=H("em"), json={})
    assert first.status_code >= 400  # the outcome of that step is unknown
    again = server.client.post(f"/api/tasks/{task_id}/publish", headers=H("em"), json={})
    assert again.status_code in (200, 201), again.text
    assert server.state(task_id) == "published"
    assert server.remote.calls.count("push") == 1 and server.remote.calls.count("create_pull") == 1
    assert len(server.remote.pulls[REPO]) == 1


def test_failure_before_a_remote_effect_is_retried_once(server):
    task_id = server.to_approved_b()
    server.remote.fail_before.add("push")
    assert server.client.post(f"/api/tasks/{task_id}/publish", headers=H("em"), json={}).status_code >= 400
    assert server.client.post(f"/api/tasks/{task_id}/publish", headers=H("em"), json={}).status_code in (200, 201)
    assert server.state(task_id) == "published" and len(server.remote.pulls[REPO]) == 1


def test_push_is_refused_when_the_remote_base_differs_from_the_prepared_tree(server):
    task_id = server.to_approved_b()
    server.remote.base["netaddr/netaddr.go"] = git_blob_id("package netaddr\n// changed on the remote\n")
    out = server.client.post(f"/api/tasks/{task_id}/publish", headers=H("em"), json={})
    assert out.status_code == 409 and "netaddr/netaddr.go differs" in out.text
    assert "push" not in server.remote.calls and server.remote.pulls.get(REPO) in (None, [])
    assert server.state(task_id) == "approved_a"
    approvals = server.wiring.approvals.rows(task_id)
    assert approvals and all(a["status"] == "voided" for a in approvals)
    notes = server.client.get("/api/notifications", headers=H("em")).json()["notifications"]
    assert any("target repository changed" in str(n).lower() for n in notes)


def test_files_the_target_keeps_for_itself_do_not_block_the_push(server):
    task_id = server.to_approved_b()
    assert server.remote.base["README.md"] != git_blob_id(base_files()["README.md"])  # differs, and is ignored
    assert server.client.post(f"/api/tasks/{task_id}/publish", headers=H("em"), json={}).status_code in (200, 201)
    del server.remote.base["go.mod"]
    second = server.to_approved_b()
    out = server.client.post(f"/api/tasks/{second}/publish", headers=H("em"), json={})
    assert out.status_code == 409 and "go.mod is missing" in out.text


class GitRemote(GitBackedPublicationAPI):
    """A local bare git repository as the remote, with the base comparison read from git itself."""

    def push_branch(self, full_name, branch, base_commit, files, message):
        # ATLAS names the base by its own tree id; the remote commits on the head it just compared.
        return super().push_branch(full_name, branch, self.base_commit, files, message)

    def base_blobs(self, full_name: str, base_ref: str) -> dict[str, str]:
        out = subprocess.run(["git", "--git-dir", str(self.bare), "ls-tree", "-r", f"refs/heads/{base_ref}"],
                             capture_output=True, text=True, check=True).stdout
        blobs = {}
        for line in out.splitlines():
            meta, path = line.split("\t", 1)
            blobs[path] = meta.split()[2]
        return blobs


def test_publication_through_the_wiring_into_a_local_bare_repository(tmp_path):
    remote = GitRemote(tmp_path / "git")
    server = Server(tmp_path / "data", remote)
    task_id = server.to_approved_b()
    out = server.client.post(f"/api/tasks/{task_id}/publish", headers=H("em"), json={})
    assert out.status_code in (200, 201), out.text
    task = server.wiring.tasks.get(task_id)
    assert remote.branch_head(REPO, task.publication_ref) == task.published_commit
    shown = subprocess.run(["git", "--git-dir", str(remote.bare), "show", f"{task.published_commit}:netaddr/netaddr.go"],
                           capture_output=True, text=True, check=True).stdout
    assert 'strings.Contains(host, ":")' in shown
