"""Fakes for the execution layer's collaborators, plus a wired test environment."""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from atlas.approvals import ApprovalService
from atlas.containment import ContainmentStore
from atlas.contracts import Action, SecurityContext, SecurityState, ValidationContext
from atlas.eligibility import Eligibility
from atlas.execution.agent import CodingAgent
from atlas.execution.corrections import CorrectionService, PullRequestBodies
from atlas.execution.db import OpsDB
from atlas.execution.ports import EvidenceRecord, PullRequest, RepoIdentity, ReviewResult, SecurityDecision
from atlas.execution.prepare import DirectoryTreeSource, Preparer
from atlas.execution.publication import Publisher
from atlas.execution.sandbox import SandboxResult
from atlas.execution.validation import ValidationService
from atlas.tasks import TaskStore
from tests.fakes import FakeClock, FakeNotifier, FakeOutbound, FakeProjection, RecordedModel

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / "target" / "repo"
REPO = "AnaghaP09/atlas-sandbox-target"
NODE_ID = "R_demo_target"

FIX_DIFF = """--- a/netaddr/netaddr.go
+++ b/netaddr/netaddr.go
@@ -1,9 +1,16 @@
 // Package netaddr formats network addresses for test helpers.
 package netaddr

-import "strconv"
+import (
+\t"strconv"
+\t"strings"
+)

 // JoinHostPort combines a host and a port into a "host:port" address.
+// IPv6 hosts are wrapped in brackets, as in "[::1]:8080".
 func JoinHostPort(host string, port int) string {
+\tif strings.Contains(host, ":") {
+\t\treturn "[" + host + "]:" + strconv.Itoa(port)
+\t}
 \treturn host + ":" + strconv.Itoa(port)
 }
"""


class FakeGate:
    def __init__(self) -> None:
        self.decisions: dict[str, SecurityDecision] = {}
        self.next_state = SecurityState.PASSED
        self.policy_version = "policy-1"
        self.revoked: set[str] = set()
        self.lifetime = timedelta(hours=24)
        self.contexts: list[SecurityContext] = []

    def decide(self, ctx: SecurityContext, now: datetime) -> SecurityDecision:
        self.contexts.append(ctx)
        d = SecurityDecision(f"sec-{len(self.decisions) + 1}", self.next_state, now + self.lifetime,
                             self.policy_version, ctx.scanned_revision)
        self.decisions[d.decision_id] = d
        return d

    def revalidate(self, decision_id: str, now: datetime) -> SecurityDecision:
        d = self.decisions[decision_id]
        if decision_id in self.revoked:
            return replace(d, state=SecurityState.NOT_RUN, reasons=("exception revoked",))
        if d.valid_until is None or d.valid_until <= now:
            return replace(d, state=SecurityState.NOT_RUN, reasons=("scan expired",))
        return replace(d, policy_version=self.policy_version)


class FakeScanner:
    def __init__(self) -> None:
        self.scans: list[SecurityContext] = []

    def scan(self, ctx: SecurityContext) -> str:
        self.scans.append(ctx)
        return f"scan-{len(self.scans)}"


class FakeRunState:
    def __init__(self) -> None:
        self.generation: dict[str, int] = {}
        self.actions: dict[tuple[str, int], frozenset[str]] = {}
        self.injection: set[str] = set()

    def authoritative_generation(self, run_id: str) -> int:
        return self.generation.get(run_id, 1)

    def verified_actions(self, run_id: str, generation: int) -> frozenset[str]:
        return self.actions.get((run_id, generation), frozenset({Action.DELEGATE_TO_CODING_AGENT.value}))

    def injection_flagged(self, run_id: str) -> bool:
        return run_id in self.injection


class FakeReviewer:
    def __init__(self, passed: bool = True) -> None:
        self.passed = passed

    def review(self, task_text, scope, diff) -> ReviewResult:
        return ReviewResult(self.passed, "matches the task" if self.passed else "does not address the task")


class PassingSandbox:
    container = True

    def __init__(self, checks: Mapping[str, str] | None = None) -> None:
        self.checks = dict(checks or {})
        self.trees: list[dict] = []

    name = "fake"
    data_mode = "REAL"

    def prepare(self, task, bundle, limits):
        return self.run_checks(bundle, limits)

    def run_checks(self, bundle, limits=None):
        tree = bundle.tree
        self.trees.append(dict(tree))
        base = {"gofmt": "passed", "go_build": "passed", "go_vet": "passed", "behavioural_tests": "passed",
                "container_sandbox": "passed"}
        base.update(self.checks)
        return SandboxResult(base, "ok", True)


class LostResponse(Exception):
    """The remote effect happened but the response was lost."""


class FakePublicationAPI:
    """Stateful fake of the remote repository: branches, draft and closed pull requests, bodies."""

    def __init__(self) -> None:
        self.identities = {REPO: RepoIdentity(NODE_ID, REPO)}
        self.branches: dict[tuple[str, str], str] = {}
        self.commits: dict[str, dict[str, str]] = {}
        self.pulls: dict[str, list[PullRequest]] = {}
        self.calls: list[str] = []
        self.lose_after: set[str] = set()
        self.fail_before: set[str] = set()
        self.body_writes = 0

    def _hook(self, name: str, before: bool) -> None:
        if before and name in self.fail_before:
            self.fail_before.discard(name)
            raise ConnectionError(f"{name} failed before reaching the remote")
        if not before and name in self.lose_after:
            self.lose_after.discard(name)
            raise LostResponse(name)

    def repo_identity(self, full_name: str) -> RepoIdentity:
        return self.identities[full_name]

    def branch_head(self, full_name: str, branch: str) -> str | None:
        self.calls.append("branch_head")
        return self.branches.get((full_name, branch))

    def push_branch(self, full_name, branch, base_commit, files, message) -> str:
        self.calls.append("push")
        self._hook("push", True)
        if (full_name, branch) in self.branches:
            raise AssertionError("second push to the same branch")
        commit = f"c{len(self.commits) + 1:039d}"
        self.commits[commit] = dict(files)
        self.branches[(full_name, branch)] = commit
        self._hook("push", False)
        return commit

    def find_pulls(self, full_name, head):
        self.calls.append("find_pulls")
        return [p for p in self.pulls.get(full_name, []) if p.head == head]

    def create_draft_pull(self, full_name, head, base, title, body):
        self.calls.append("create_pull")
        self._hook("create_pull", True)
        if self.find_pulls(full_name, head):
            raise AssertionError("second pull request for the same head")
        pr = PullRequest(len(self.pulls.get(full_name, [])) + 1, head, base, title, body)
        self.pulls.setdefault(full_name, []).append(pr)
        self._hook("create_pull", False)
        return pr

    def get_pull(self, full_name, number):
        self.calls.append("get_pull")
        self._hook("get_pull", True)
        return next(p for p in self.pulls[full_name] if p.number == number)

    def replace_pull_body(self, full_name, number, body):
        self.calls.append("replace_body")
        self._hook("replace_body", True)
        pr = next(p for p in self.pulls[full_name] if p.number == number)
        pr.body = body
        self.body_writes += 1
        self._hook("replace_body", False)


class GitBackedPublicationAPI(FakePublicationAPI):
    """Pushes real commits into a local bare git repository (hooks disabled)."""

    def __init__(self, workdir: Path) -> None:
        super().__init__()
        self.bare = workdir / "target.git"
        self.work = workdir / "seed"
        self._git("init", "-q", "--bare", str(self.bare))
        self._git("clone", "-q", str(self.bare), str(self.work))
        for path in TARGET.rglob("*"):
            if path.is_file():
                dest = self.work / path.relative_to(TARGET)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(path.read_bytes())
        self._git("-C", str(self.work), "add", "-A")
        self._git("-C", str(self.work), "-c", "user.name=atlas", "-c", "user.email=atlas@users.noreply.github.com",
                  "commit", "-q", "-m", "base")
        self._git("-C", str(self.work), "push", "-q", "origin", "HEAD:main")
        self.base_commit = self._git("-C", str(self.work), "rev-parse", "HEAD").strip()

    @staticmethod
    def _git(*args: str) -> str:
        return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], check=True, capture_output=True,
                              text=True).stdout

    def branch_head(self, full_name, branch):
        out = subprocess.run(["git", "--git-dir", str(self.bare), "rev-parse", "--verify", "-q", f"refs/heads/{branch}"],
                             capture_output=True, text=True)
        return out.stdout.strip() or None

    def push_branch(self, full_name, branch, base_commit, files, message):
        self.calls.append("push")
        self._hook("push", True)
        if self.branch_head(full_name, branch):
            raise AssertionError("second push to the same branch")
        self._git("-C", str(self.work), "checkout", "-q", "--detach", base_commit)
        for rel, content in files.items():
            p = self.work / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8", newline="\n")
        self._git("-C", str(self.work), "add", "-A")
        self._git("-C", str(self.work), "-c", "user.name=atlas", "-c", "user.email=atlas@users.noreply.github.com",
                  "commit", "-q", "-m", message)
        self._git("-C", str(self.work), "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
        commit = self._git("-C", str(self.work), "rev-parse", "HEAD").strip()
        self._hook("push", False)
        return commit


class InMemoryEvidence:
    def __init__(self) -> None:
        self.rows: dict[tuple, list[EvidenceRecord]] = {}

    def read(self, ctx: ValidationContext, kind: str):
        return list(self.rows.get((ctx, kind), []))

    def write(self, ctx: ValidationContext, kind: str, data: Mapping[str, Any], writer: str) -> None:
        self.rows.setdefault((ctx, kind), []).append(EvidenceRecord(kind, dict(data), datetime.now()))


@dataclass
class Env:
    db: OpsDB
    clock: FakeClock
    tasks: TaskStore
    approvals: ApprovalService
    containment: ContainmentStore
    gate: FakeGate
    scanner: FakeScanner
    run_state: FakeRunState
    eligibility: Eligibility
    notifier: FakeNotifier
    projection: FakeProjection
    outbound: FakeOutbound
    sandbox: Any
    reviewer: FakeReviewer
    preparer: Preparer
    api: FakePublicationAPI
    publisher: Publisher
    evidence: InMemoryEvidence
    validation: ValidationService
    corrections: CorrectionService
    revision: list = field(default_factory=lambda: ["rev-1"])

    @property
    def now(self) -> datetime:
        return self.clock.now()

    def new_task(self, *, action: str = Action.DELEGATE_TO_CODING_AGENT.value, run_id: str = "run-1",
                 generation: int = 1, base_commit: str = "base000", key: str | None = None):
        return self.tasks.create(
            run_id=run_id, generation=generation, action=action, task_class="test_adjustment",
            task_text="Fix host:port formatting for IPv6 in netaddr.JoinHostPort",
            scope=["netaddr/netaddr.go"], destination_repo=REPO, destination_node_id=NODE_ID, base_ref="main",
            base_commit=base_commit, snapshot_id="snap-g05", policy_version="policy-1", scanner=self.scanner,
            gate=self.gate, now=self.now, idempotency_key=key,
        )

    def to_prepared(self, task=None, **kw):
        task = task or self.new_task(**kw)
        self.approvals.grant_a(task.task_id, role="em", eligibility=self.eligibility, now=self.now)
        self.preparer.prepare(task.task_id, self.now)
        return self.tasks.get(task.task_id)

    def to_approved_b(self, task=None, **kw):
        task = self.to_prepared(task, **kw)
        self.approvals.grant_b(task.task_id, role="em", eligibility=self.eligibility, now=self.now)
        return self.tasks.get(task.task_id)

    def to_published(self, task=None, **kw):
        task = self.to_approved_b(task, **kw)
        self.publisher.publish(task.task_id, self.now)
        return self.tasks.get(task.task_id)


def make_env(*, sandbox=None, api=None, db_path=None, diff: str = FIX_DIFF) -> Env:
    db = OpsDB(db_path or ":memory:")
    clock = FakeClock()
    notifier = FakeNotifier()
    revision = ["rev-1"]
    containment = ContainmentStore(db, deployed_revision=lambda: revision[0])
    tasks = TaskStore(db)
    approvals = ApprovalService(db, tasks, notifier, containment)
    gate, scanner, run_state = FakeGate(), FakeScanner(), FakeRunState()
    eligibility = Eligibility(tasks, approvals, gate, run_state, containment)
    projection = FakeProjection()
    outbound = FakeOutbound(RecordedModel(default={"diff": diff}))
    sandbox = sandbox or PassingSandbox()
    reviewer = FakeReviewer()
    trees = DirectoryTreeSource(TARGET)
    preparer = Preparer(tasks, eligibility, CodingAgent(outbound), sandbox, reviewer, projection, trees, containment)
    api = api or FakePublicationAPI()
    publisher = Publisher(db, tasks, approvals, eligibility, api, projection, preparer.base_tree, containment,
                          upstream_repositories={"example/upstream"})
    evidence = InMemoryEvidence()
    validation = ValidationService(tasks, evidence)
    corrections = CorrectionService(db, containment, PullRequestBodies(api), notifier)
    return Env(db, clock, tasks, approvals, containment, gate, scanner, run_state, eligibility, notifier, projection,
               outbound, sandbox, reviewer, preparer, api, publisher, evidence, validation, corrections, revision)
