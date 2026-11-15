"""Publication: one draft pull request per task, written step by step under a durable journal.

Steps are ``push``, ``pull_request`` and ``description``. Before each remote effect the
eligibility predicate is re-evaluated (approvals, security decision now, generation, containment,
checks, outgoing-commit match) and an ``intent`` row is committed; after it a ``done`` row records
the remote id. The predicate, the intent row and the remote effect happen under the database lock,
so a concurrent re-verification either completes before the step or makes the step refuse. On a
restart, any step left in ``intent`` is reconciled read-only (branch, pull requests by head
including closed ones, description) before anything is retried: never a second push, never a
second pull request; a closed pull request or an unresolvable outcome stops for a person.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable

from atlas.approvals import ApprovalService
from atlas.containment import ContainmentStore
from atlas.contracts import Projection, TaskState
from atlas.eligibility import Eligibility
from atlas.execution import artefact as artefacts
from atlas.execution.db import OpsDB, iso
from atlas.execution.ports import PublicationAPI
from atlas.tasks import TaskStore

STEPS = ("push", "pull_request", "description")
BASE_CHANGED = "The target repository changed after the task was prepared. Both approvals are void; prepare the task again."


def git_blob_id(text: str) -> str:
    """The id git gives a file with this content (SHA-1 over a ``blob`` header and the bytes)."""
    import hashlib

    data = text.encode("utf-8")
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()  # noqa: S324 - a git object id


class PublicationRefused(RuntimeError):
    def __init__(self, reasons) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = tuple(reasons)


class PublicationStopped(RuntimeError):
    """A remote outcome could not be established, or a closed pull request was found."""


class Publisher:
    def __init__(self, db: OpsDB, tasks: TaskStore, approvals: ApprovalService, eligibility: Eligibility,
                 api: PublicationAPI, projection: Projection, base_tree: Callable[[Any], dict[str, str]],
                 containment: ContainmentStore,
                 publish_grant: Callable[[str | None, str], bool] | None = None,
                 upstream_repositories: frozenset[str] | set[str] = frozenset(),
                 notifier: Any = None, not_compared: frozenset[str] | set[str] = frozenset()) -> None:
        self.db = db
        self.tasks = tasks
        self.approvals = approvals
        self.eligibility = eligibility
        self.api = api
        self.projection = projection
        self.base_tree = base_tree
        self.containment = containment
        # The project's export grant (publish_targets in the projects file), checked before every step.
        self.publish_grant = publish_grant
        # Repositories ATLAS reads from (from configuration and the projects file); never written to.
        self.upstream = {r.lower() for r in upstream_repositories}
        self.notifier = notifier
        # Files the target repository keeps for itself; they are left out of the base comparison.
        self.not_compared = set(not_compared)

    # -- journal -------------------------------------------------------------------------------

    def journal(self, action_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.query("SELECT * FROM journal WHERE action_id = ? ORDER BY seq", (action_id,))]

    def _last(self, action_id: str, step: str) -> dict[str, Any] | None:
        rows = [r for r in self.journal(action_id) if r["step"] == step]
        return rows[-1] if rows else None

    def _write(self, action_id: str, step: str, status: str, now: datetime, remote_id: str | None = None,
               detail: dict | None = None) -> None:
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO journal (action_id, step, status, remote_id, detail_json, at) VALUES (?,?,?,?,?,?)",
                         (action_id, step, status, remote_id, json.dumps(detail or {}, sort_keys=True), iso(now)))

    def complete(self, action_id: str) -> bool:
        last = self._last(action_id, "description")
        return bool(last and last["status"] == "done")

    # -- outgoing-commit match -------------------------------------------------------------------

    def outgoing_problems(self, task) -> list[str]:
        frozen = task.artefact or {}
        changes = [artefacts.SourceChange(s["path"], s["old_path"], s["hunks"], s["required_scopes"])
                   for s in frozen.get("source", [])]
        return artefacts.verify_outgoing(
            self.projection, frozen, self.base_tree(task), changes,
            commit_message=frozen.get("commit_message", ""), title=frozen.get("title", ""), body=frozen.get("body", ""),
            field_scopes=frozen.get("field_scopes") or None, project_id=task.project_id or None,
        )

    # -- base match ------------------------------------------------------------------------------

    def base_problems(self, task) -> list[str]:
        """Files of the prepared base tree that are missing or different on the remote base branch.

        The change was prepared against the local copy of the target. If the remote base has moved
        away from it, the frozen change no longer says what would really be published.
        """
        if not hasattr(self.api, "base_blobs"):
            return []
        remote = self.api.base_blobs(task.destination_repo, task.base_ref)
        problems = []
        for path, text in sorted(self.base_tree(task).items()):
            if path in self.not_compared:
                continue
            if path not in remote:
                problems.append(f"{path} is missing on the base branch of the target")
            elif remote[path] != git_blob_id(text):
                problems.append(f"{path} differs on the base branch of the target")
        return problems

    def _refuse_base(self, task_id: str, problems: list[str], now: datetime) -> None:
        task = self.tasks.get(task_id)
        self.approvals.void_task(task_id, "the base branch of the target no longer matches the prepared change", now)
        with self.db.transaction() as conn:
            if self.tasks.get(task_id).state == TaskState.APPROVED_B:
                self.tasks.transition(task_id, TaskState.APPROVED_A, "target base changed; prepare again", now, conn=conn)
        if self.notifier is not None:
            content = {"text": BASE_CHANGED, "task_id": task_id, "required_scopes": ["internal"]}
            try:
                self.notifier.emit("approval.voided", task_id, [task_id], frozenset({"internal"}), content)
            except Exception:  # noqa: BLE001 - a notification fault must not hide the refusal
                pass
        raise PublicationRefused(problems)

    def _gate(self, task_id: str, now: datetime) -> None:
        task = self.tasks.get(task_id)
        if task.destination_repo.lower() in self.upstream:
            raise PublicationRefused(["publication to an upstream project repository is never allowed"])
        if task.support_status == "withdrawn":
            raise PublicationRefused(["support for the task was withdrawn"])
        if self.publish_grant is not None and not self.publish_grant(task.project_id or None, task.destination_repo):
            raise PublicationRefused([f"the project has no export grant to publish to {task.destination_repo}"])
        mismatch = self.outgoing_problems(task)
        if mismatch:
            self.approvals.void_task(task_id, "artefact changed after Approval B", now)
            self.containment.trip(2, producer="publication", now=now,
                                  detail={"task_id": task_id, "run_id": task.run_id, "problems": mismatch})
            with self.db.transaction() as conn:
                if self.tasks.get(task_id).state == TaskState.APPROVED_B:
                    self.tasks.transition(task_id, TaskState.APPROVED_A, "artefact mismatch; prepare again", now, conn=conn)
            raise PublicationRefused(mismatch)
        verdict = self.eligibility.evaluate(task_id, "publication_step", now, {"outgoing_commit_match": "passed"})
        if not verdict.allowed:
            raise PublicationRefused(verdict.reasons)
        identity = self.api.repo_identity(task.destination_repo)
        problems = self.approvals.verify_destination(task, identity.node_id, identity.full_name, task.publication_ref, now)
        if problems:
            raise PublicationRefused(problems)

    # -- reconciliation --------------------------------------------------------------------------

    def reconcile(self, task_id: str, now: datetime) -> list[str]:
        """Resolve steps left in ``intent`` using read-only remote queries."""
        task = self.tasks.get(task_id)
        notes = []
        repo = task.destination_repo
        frozen = task.artefact or {}
        for step in STEPS:
            last = self._last(task.action_id, step)
            if not last or last["status"] != "intent":
                continue
            try:
                if step == "push":
                    head = self.api.branch_head(repo, task.publication_ref)
                    if head:
                        self._write(task.action_id, "push", "done", now, head, {"reconciled": True})
                        notes.append("push found on the remote")
                    else:
                        self._write(task.action_id, "push", "absent", now, None, {"reconciled": True})
                        notes.append("push had not happened")
                elif step == "pull_request":
                    pulls = self.api.find_pulls(repo, task.publication_ref)
                    open_ = [p for p in pulls if p.state == "open"]
                    if open_:
                        self._write(task.action_id, "pull_request", "done", now, str(open_[0].number), {"reconciled": True})
                        notes.append("pull request found on the remote")
                    elif pulls:
                        self._write(task.action_id, "pull_request", "stopped", now, str(pulls[0].number),
                                    {"reason": "closed pull request found"})
                        raise PublicationStopped("a closed pull request exists for this task; a person must decide")
                    else:
                        self._write(task.action_id, "pull_request", "absent", now, None, {"reconciled": True})
                        notes.append("pull request had not been created")
                elif step == "description":
                    pr_row = self._last(task.action_id, "pull_request")
                    number = int(pr_row["remote_id"])
                    if self.api.get_pull(repo, number).body == frozen.get("body"):
                        self._write(task.action_id, "description", "done", now, str(number), {"reconciled": True})
                        notes.append("description already written")
                    else:
                        self._write(task.action_id, "description", "absent", now, None, {"reconciled": True})
            except PublicationStopped:
                raise
            except Exception as exc:  # noqa: BLE001 - an unknown outcome stops for a person
                self._write(task.action_id, step, "stopped", now, None, {"reason": f"reconciliation failed: {exc}"})
                raise PublicationStopped(f"could not establish the outcome of {step}: {exc}") from exc
        return notes

    # -- publish -------------------------------------------------------------------------------

    def publish(self, task_id: str, now: datetime) -> dict[str, Any]:
        task = self.tasks.get(task_id)
        for row in self.journal(task.action_id):
            if row["status"] == "stopped":
                raise PublicationStopped(json.loads(row["detail_json"]).get("reason", "stopped for a person"))
        with self.db.lock:
            self.reconcile(task_id, now)
            for step in STEPS:
                last = self._last(task.action_id, step)
                if last and last["status"] == "done":
                    continue
                self._gate(task_id, now)
                task = self.tasks.get(task_id)
                frozen = task.artefact or {}
                if step == "push":
                    drift = self.base_problems(task)
                    if drift:
                        self._refuse_base(task_id, drift, now)
                self._write(task.action_id, step, "intent", now)
                if step == "push":
                    commit = self.api.push_branch(task.destination_repo, task.publication_ref, task.base_commit,
                                                  frozen["changed_files"], frozen["commit_message"])
                    self._write(task.action_id, step, "done", now, commit)
                elif step == "pull_request":
                    pr = self.api.create_draft_pull(task.destination_repo, task.publication_ref, task.base_ref,
                                                    frozen["title"], "")
                    self._write(task.action_id, step, "done", now, str(pr.number))
                else:
                    number = int(self._last(task.action_id, "pull_request")["remote_id"])
                    self.api.replace_pull_body(task.destination_repo, number, frozen["body"])
                    self._write(task.action_id, step, "done", now, str(number))
            task = self.tasks.get(task_id)
            commit = self._last(task.action_id, "push")["remote_id"]
            number = int(self._last(task.action_id, "pull_request")["remote_id"])
            if task.state == TaskState.APPROVED_B:
                self.tasks.mark_published(task_id, commit=commit, pr_number=number, now=now)
        return {"commit": commit, "pr_number": number, "publication_id": f"pr:{task.destination_repo}#{number}"}
