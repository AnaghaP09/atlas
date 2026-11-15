"""Preparation: from an approved task to a frozen public artefact with recorded check results.

Steps: the eligibility predicate for preparation; the coding agent proposes a diff; non-executing
checks on the raw diff; the public artefact is built and frozen; the sandbox runs its checks on the
artefact's tree; the verifier reviews the artefact diff; results are recorded and the task moves to
``prepared``. Failed or unavailable checks are recorded as such; Approval B then cannot be given.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Mapping, Protocol, Sequence

from atlas.containment import ContainmentStore
from atlas.contracts import Projection, TaskState
from atlas.eligibility import Eligibility
from atlas.execution import artefact as artefacts
from atlas.execution import diffs
from atlas.execution.agent import CodingAgent
from atlas.execution.checks import static_checks
from atlas.execution.ports import DiffReviewer
from atlas.execution.runner import IsolationViolation, SandboxBundle, SandboxLimits
from atlas.tasks import TaskStore


from atlas.execution.target import summary_line


class TreeSource(Protocol):
    def files(self, repository: str, commit: str) -> dict[str, str]:
        """Text files of the repository at a commit."""


class DirectoryTreeSource:
    """Reads a local checkout (the demo target content) as the base tree."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def files(self, repository: str, commit: str) -> dict[str, str]:
        out = {}
        for path in self.root.rglob("*"):
            if path.is_file() and ".git" not in path.parts:
                out[path.relative_to(self.root).as_posix()] = path.read_text(encoding="utf-8")
        return out


class PreparationRefused(RuntimeError):
    def __init__(self, reasons: Sequence[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = tuple(reasons)


class Preparer:
    def __init__(self, tasks: TaskStore, eligibility: Eligibility, agent: CodingAgent, sandbox,
                 reviewer: DiffReviewer, projection: Projection, trees: TreeSource,
                 containment: ContainmentStore, limits: SandboxLimits | None = None) -> None:
        self.limits = limits or SandboxLimits()
        self.tasks = tasks
        self.eligibility = eligibility
        self.agent = agent
        self.sandbox = sandbox
        self.reviewer = reviewer
        self.projection = projection
        self.trees = trees
        self.containment = containment

    def describe(self, task) -> tuple[str, str, str]:
        summary = summary_line(task.task_text)
        message = f"Fix: {summary}"
        title = f"Draft: {summary}"
        body = (f"{task.task_text}\n\nPrepared by ATLAS for task {task.action_id}. This draft was produced by a "
                "coding agent in an isolated sandbox and approved by a person before publication. Review before "
                "merging; nothing is merged automatically.")
        return message, title, body

    def prepare(self, task_id: str, now: datetime, *, source_scopes: Mapping[str, Sequence[str]] | None = None,
                field_scopes: Mapping[str, Sequence[str]] | None = None) -> dict:
        verdict = self.eligibility.evaluate(task_id, "preparation", now)
        if not verdict.allowed:
            raise PreparationRefused(verdict.reasons)
        task = self.tasks.get(task_id)
        if task.state != TaskState.APPROVED_A:
            raise PreparationRefused([f"task is {task.state.value}, not approved_a"])
        base = self.trees.files(task.destination_repo, task.base_commit)
        diff_text = self.propose_valid(task, base)
        parsed = diffs.parse(diff_text)
        results = {c.name: c.status for c in static_checks(parsed, task.scope)}

        changes = artefacts.source_from_diff(diff_text)
        if source_scopes:
            changes = [
                artefacts.SourceChange(c.path, c.old_path,
                                       [{"text": h["text"], "required_scopes": list(source_scopes.get(f"{c.path}#{i}", h["required_scopes"]))}
                                        for i, h in enumerate(c.hunks)],
                                       list(source_scopes.get(c.path, c.required_scopes)))
                for c in changes
            ]
        message, title, body = self.describe(task)
        frozen = artefacts.build(self.projection, base, changes, commit_message=message, title=title, body=body,
                                 field_scopes=field_scopes, project_id=task.project_id or None)
        frozen["source"] = [{"path": c.path, "old_path": c.old_path, "hunks": [dict(h) for h in c.hunks],
                             "required_scopes": list(c.required_scopes)} for c in changes]
        frozen["field_scopes"] = {k: list(v) for k, v in (field_scopes or {}).items()}
        results["secret_scan"] = "passed" if results["secret_scan"] == "passed" and frozen["scans"]["secret_scan"] == "passed" else "failed"
        results["personal_data_scan"] = ("passed" if results["personal_data_scan"] == "passed"
                                         and frozen["scans"]["personal_data_scan"] == "passed" else "failed")

        tree = diffs.apply(base, diffs.parse(frozen["diff"]))
        try:
            sandbox_result = self.sandbox.prepare(task, SandboxBundle(tree), self.limits)
        except IsolationViolation as exc:
            self.containment.trip(3, producer="sandbox", now=now,
                                  detail={"task_id": task_id, "run_id": task.run_id, "violation": str(exc)})
            raise PreparationRefused([f"sandbox isolation violated: {exc}"]) from exc
        results.update(sandbox_result.checks)
        review = self.reviewer.review(task.task_text, task.scope, frozen["diff"])
        results["verifier_review"] = "passed" if review.passed else "failed"

        with self.tasks.db.transaction() as conn:
            current = self.tasks.get(task_id)
            if current.state != TaskState.APPROVED_A:
                raise PreparationRefused([f"task changed to {current.state.value} during preparation"])
            self.tasks.update_fields(task_id, now, conn=conn, artefact_json=json.dumps(frozen, sort_keys=True),
                                     checks_json=json.dumps(results, sort_keys=True))
            self.tasks.transition(task_id, TaskState.PREPARED, "prepared", now, conn=conn)
        return {"checks": results, "artefact": {k: frozen[k] for k in ("diff_sha256", "tree_sha256", "withheld")},
                "sandbox_log": sandbox_result.log[-4000:], "container": sandbox_result.container}

    def propose_valid(self, task, base: Mapping[str, str], attempts: int = 2) -> str:
        """Ask the coding agent for a diff that parses and applies to the base; one more try if it does not."""
        last: Exception | None = None
        for _ in range(attempts):
            diff_text = diffs.recount(self.agent.propose(task.task_text, task.scope, base))
            try:
                diffs.apply(dict(base), diffs.parse(diff_text))
                return diff_text
            except diffs.DiffError as exc:
                last = exc
        raise PreparationRefused([f"the coding agent did not return a usable change: {last}"])

    def base_tree(self, task) -> dict[str, str]:
        return self.trees.files(task.destination_repo, task.base_commit)

    def source_changes(self, task) -> list[artefacts.SourceChange]:
        return [artefacts.SourceChange(s["path"], s["old_path"], s["hunks"], s["required_scopes"])
                for s in (task.artefact or {}).get("source", [])]
