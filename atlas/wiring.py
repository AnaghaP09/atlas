"""Connect the running server to the real layers.

``connect(services, settings)`` is called once at start-up by ``python -m atlas.serve``. It builds
the outbound boundary (with the configured model provider), the run engine (Scout and Sentinel),
the execution layer (task store, approvals, eligibility, containment, sandbox runner) and plugs
them into the API through its ports.

The evidence side (snapshot registry, live stores per project, security scans for tasks, the
forecast's open items and the live dashboard pollers) comes from ``atlas.data_wiring``. Without a
data directory (in-memory test runs) the server falls back to empty stand-ins that say so plainly.

Publication (one branch and one draft pull request in the configured target repository) is
connected when a publish token and a target are set. Validation evidence is kept in the data
layer's action store, and a monitor reads the target's CI results for every published task.

What is not connected in this build is said plainly instead of faked:

* **Corrections.** Replacing a published description during an incident answers "not connected".
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from atlas.api.ports import NotWired, Refused
from atlas.approvals import ApprovalError, ApprovalService
from atlas.config import parse_env_file
from atlas.containment import (BLOCKS_IN_WORDS, ROW_BY_SWITCH_ID, RULES, SWITCHES, ContainmentError,
                               ContainmentStore)
from atlas.feed import ProjectFeed
from atlas.data_wiring import connect_data
from atlas.projects import ProjectRegistry
from atlas.contracts import (
    REVERIFICATION_ASSESSMENTS,
    ApprovalKind,
    EgressPurpose,
    Record,
    ReviewResult,
    RunContext,
    SecurityContext,
    SecurityDecision,
    SecurityState,
    TaskState,
)
from atlas.eligibility import Eligibility
from atlas.execution.agent import CodingAgent
from atlas.execution.db import OpsDB
from atlas.execution.lifecycle import RefreshError, apply_reverification, security_refresh
from atlas.execution.github_publication import GitHubPublicationAPI, PublicationUnavailable
from atlas.execution.prepare import DirectoryTreeSource, PreparationRefused, Preparer
from atlas.execution.publication import PublicationRefused, PublicationStopped, Publisher
from atlas.gateway import PublishGrant
from atlas.execution.runner import select_runner
from atlas.execution.target import CHANGE_HEADING, NoTargetMapping, TargetManifest, change_section
from atlas.execution.validation import ValidationService
from atlas.execution.validation_store import StoreValidationEvidence, TargetMonitor
from atlas.orchestrator import Deps, Orchestrator, RunStore
from atlas.outbound import CeilingGate, OutboundBoundary, SpendMeter, provider_from_env
from atlas.scorecard import Scorecard
from atlas.security_gate import GateResult, SecurityGate as RunSecurityGate, revalidate as gate_revalidate
from atlas.tasks import TaskError, TaskStore

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TARGET_DIR = ROOT / "target" / "repo"
POLICY_VERSION = "policy-1"
NOT_CONNECTED_DATA = "the data layer is not connected in this build yet"
NOT_CONNECTED_PUBLICATION = "publishing to the target repository is not connected in this build yet"
NOT_CONNECTED_CORRECTIONS = "replacing a published description during an incident is not connected in this build yet"

# Outbound containment events -> containment table rows.
EVENT_ROWS = {"outbound_offline_attempt": 4, "outbound_not_allowlisted": 5, "outbound_unredacted_pii": 6}
# Gateway violations -> the same containment events.
GATEWAY_EVENTS = {"offline_egress_attempt": "outbound_offline_attempt", "allow_list_violation": "outbound_not_allowlisted"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------------------------
# Local stand-ins for the data layer (empty; nothing is invented)
# --------------------------------------------------------------------------------------------


class EmptyEvidenceStore:
    """An evidence store with nothing in it. Every read answers "not found"."""

    data_mode = "STAGED"

    def put(self, record: Record, writer: str) -> None:
        raise NotWired(NOT_CONNECTED_DATA)

    def get(self, ctx: RunContext, source: str, source_id: str) -> Record | None:
        return None

    def get_record(self, ctx: RunContext, record_id: str) -> Record | None:
        return None

    def read_content(self, ctx: RunContext, record: Record) -> str:
        raise PermissionError("record is not visible in this snapshot")

    def history(self, ctx: RunContext, source: str, source_id: str) -> list[Record]:
        return []

    def search(self, ctx: RunContext, query: Mapping[str, Any]) -> list[Record]:
        return []


class LocalSnapshots:
    """Snapshot port without a data layer: snapshots can be frozen, but they are empty."""

    def __init__(self, path: Path | None, default_project: str | None, clock: Callable[[], datetime] = _now) -> None:
        self.path = path
        self.default_project = default_project
        self.clock = clock
        self._lock = threading.Lock()
        self._snapshots: dict[str, dict[str, Any]] = {}
        if path is not None and path.exists():
            self._snapshots = json.loads(path.read_text(encoding="utf-8"))

    def _save(self) -> None:
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._snapshots, indent=1, sort_keys=True), encoding="utf-8")

    def ingest(self, sources: Sequence[str], project_id: str) -> Mapping[str, Any]:
        raise NotWired(NOT_CONNECTED_DATA)

    def freeze(self, project_id: str) -> Mapping[str, Any]:
        sid = f"snap-local-{uuid.uuid4().hex[:8]}"
        snap = {"snapshot_id": sid, "project_id": project_id or self.default_project, "release": None,
                "kind": "empty", "cutoff": self.clock().isoformat(), "alias_variant": None, "mode": "replay",
                "counts": {"records": 0}, "data_mode": "STAGED",
                "note": "This snapshot holds no evidence: " + NOT_CONNECTED_DATA + "."}
        with self._lock:
            self._snapshots[sid] = snap
            self._save()
        return snap

    def list(self, release: str | None = None, project_id: str | None = None) -> list[Mapping[str, Any]]:
        return [s for s in self._snapshots.values()
                if (release is None or s.get("release") == release) and (project_id is None or s.get("project_id") == project_id)]

    def get(self, snapshot_id: str) -> Mapping[str, Any] | None:
        return self._snapshots.get(snapshot_id)

    def graph(self, snapshot_id: str) -> Mapping[str, Any]:
        if snapshot_id not in self._snapshots:
            return {"nodes": [], "edges": []}
        return {"nodes": [], "edges": [], "note": NOT_CONNECTED_DATA}

    def evidence(self, snapshot_id: str, record_id: str, content_version_id: str | None,
                 lineage: Sequence[str] = ()) -> Mapping[str, Any] | None:
        return None


class ActionSecurityPartition:
    """Per-task security records. Without the scanner connector nothing is ever written here."""

    def __init__(self) -> None:
        self._records: dict[SecurityContext, list[Record]] = {}

    def read(self, ctx: SecurityContext) -> list[Record]:
        return list(self._records.get(ctx, []))

    def write(self, ctx: SecurityContext, record: Record) -> None:
        self._records.setdefault(ctx, []).append(record)


class UnconnectedScanner:
    """Scanner stand-in: records that no scan ran, so the gate answers ``not_run``."""

    def scan(self, ctx: SecurityContext) -> str:
        return f"scan-not-run-{uuid.uuid4().hex[:6]}"


# --------------------------------------------------------------------------------------------
# Security decisions for tasks (execution-layer port over the real gate)
# --------------------------------------------------------------------------------------------


class TaskSecurityGate:
    """Implements the execution layer's ``decide``/``revalidate`` over the real security gate.

    Decisions are kept by id; ``revalidate`` re-checks expiry and policy with the real rules.
    """

    def __init__(self, reader: ActionSecurityPartition, components: Callable[[SecurityContext], Sequence[str]],
                 clock: Callable[[], datetime] = _now, policy_version: str = POLICY_VERSION) -> None:
        self.reader = reader
        self.components = components
        self.gate = RunSecurityGate(policy_version, clock)
        self.policy_version = policy_version
        self._results: dict[str, tuple[GateResult, str]] = {}

    def decide(self, ctx: SecurityContext, now: datetime) -> SecurityDecision:
        result = self.gate.evaluate_for_task(self.reader, ctx, self.components(ctx))
        reasons = tuple(result.reasons) or (("no successful scan for this task", ) if result.state == SecurityState.NOT_RUN else ())
        self._results[result.decision_id] = (result, ctx.scanned_revision)
        return SecurityDecision(result.decision_id, result.state, result.valid_until, result.policy_version,
                                ctx.scanned_revision, reasons=reasons)

    def revalidate(self, decision_id: str, now: datetime) -> SecurityDecision:
        stored = self._results.get(decision_id)
        if stored is None:
            return SecurityDecision(decision_id, SecurityState.NOT_RUN, None, self.policy_version, "",
                                    reasons=("unknown security decision",))
        result, revision = stored
        ok, reason = gate_revalidate(result, now, self.policy_version, lambda _id: None, False)
        state = result.state if ok else SecurityState.NOT_RUN
        return SecurityDecision(decision_id, state, result.valid_until, result.policy_version, revision,
                                reasons=() if ok else (reason,))


# --------------------------------------------------------------------------------------------
# Model calls for the coding agent and the diff reviewer
# --------------------------------------------------------------------------------------------


DIFF_SCHEMA = {"type": "object", "properties": {"diff": {"type": "string"}}, "required": ["diff"],
               "additionalProperties": False}
REVIEW_SCHEMA = {"type": "object", "properties": {"passed": {"type": "boolean"}, "reason": {"type": "string"}},
                 "required": ["passed", "reason"], "additionalProperties": False}


class MessageOutbound:
    """Turns a plain request payload into the boundary's message form for one role."""

    def __init__(self, boundary: OutboundBoundary, schema: Mapping[str, Any]) -> None:
        self.boundary = boundary
        self.schema = schema

    def call(self, role: str, purpose: EgressPurpose, payload: Mapping[str, Any]) -> Any:
        if "messages" in payload:
            return self.boundary.call(role, purpose, payload)
        body = {k: v for k, v in payload.items() if k != "instructions"}
        wrapped = {"system": str(payload.get("instructions", "")),
                   "messages": [{"role": "user", "content": json.dumps(body, sort_keys=True)}],
                   "schema": self.schema, "max_tokens": 8000}
        return self.boundary.call(role, purpose, wrapped)


class SentinelDiffReviewer:
    """Independent review of a prepared diff by the verifier (Sentinel)."""

    INSTRUCTIONS = ("You are reviewing a code change against its task. Pass it only if the diff does what "
                    "the task asks, stays inside the permitted scope and changes nothing else. Judge the diff "
                    "against the 'change' text. The 'background' text describes the cause found in another "
                    "project; the diff does not have to address it.")

    def __init__(self, outbound: MessageOutbound) -> None:
        self.outbound = outbound

    def review(self, task_text: str, scope: Sequence[str], diff: str) -> ReviewResult:
        r = self.outbound.call("verifier", EgressPurpose.MODEL_CALL,
                               {"instructions": self.INSTRUCTIONS, "change": change_section(task_text),
                                "background": task_text, "scope": list(scope), "diff": diff})
        content = r.content if isinstance(r.content, Mapping) else {}
        return ReviewResult(bool(content.get("passed")), str(content.get("reason", "no reason given")))


class ValidationEvidenceMemory:
    """Validation evidence held in memory until the data layer's validation store is connected."""

    def __init__(self) -> None:
        self.rows: dict[tuple, list[Any]] = {}

    def read(self, ctx: Any, kind: str) -> list[Any]:
        return list(self.rows.get((ctx, kind), []))

    def write(self, ctx: Any, kind: str, data: Mapping[str, Any], writer: str) -> None:
        from atlas.contracts import EvidenceRecord

        self.rows.setdefault((ctx, kind), []).append(EvidenceRecord(kind, dict(data), _now()))


class UnconnectedPublicationAPI:
    def __getattr__(self, name: str) -> Any:
        def _missing(*args: Any, **kwargs: Any) -> Any:
            raise NotWired(NOT_CONNECTED_PUBLICATION)

        return _missing


# --------------------------------------------------------------------------------------------
# Run state and re-verification between the agents layer and the execution layer
# --------------------------------------------------------------------------------------------


class RunStateAdapter:
    """The execution layer's view of runs, read from the orchestrator's run store."""

    def __init__(self, runs: RunStore) -> None:
        self.runs = runs

    def authoritative_generation(self, run_id: str) -> int:
        auth = self.runs.authoritative(run_id)
        return auth[0] if auth else 0

    def verified_actions(self, run_id: str, generation: int) -> frozenset[str]:
        artefact = self.runs.generation(run_id, generation) or {}
        value = (artefact.get("action") or {}).get("value")
        return frozenset({value}) if value else frozenset()

    def injection_flagged(self, run_id: str) -> bool:
        auth = self.runs.authoritative(run_id)
        return bool(auth and auth[1].get("injection_detected"))


class ExecutionDisposition:
    """Maps the run engine's per-task assessments onto the execution layer's re-verification.

    The run engine reports one assessment per task (``still_supported``, ``unsupported`` or
    ``verification_failed``); the execution layer takes the set of still-supported actions and
    whether verification worked, and applies each task's disposition by publication phase.
    """

    def __init__(self, tasks: TaskStore, approvals: ApprovalService, publisher_complete: Callable[[str], bool],
                 notifier: Any, clock: Callable[[], datetime] = _now) -> None:
        self.tasks = tasks
        self.approvals = approvals
        self.publisher_complete = publisher_complete
        self.notifier = notifier
        self.clock = clock

    def tasks_for_run(self, run_id: str) -> list[Mapping[str, Any]]:
        return [{"task_id": t.task_id, "action": t.executable_action, "state": t.state.value}
                for t in self.tasks.for_run(run_id)]

    def apply_reverification(self, run_id: str, new_generation: int, assessments: Mapping[str, str]) -> None:
        unknown = set(assessments.values()) - set(REVERIFICATION_ASSESSMENTS)
        if unknown:
            raise ValueError(f"unknown assessments: {sorted(unknown)}")
        tasks = {t["task_id"]: t for t in self.tasks_for_run(run_id)}
        supported = frozenset(tasks[tid]["action"] for tid, a in assessments.items()
                              if a == "still_supported" and tid in tasks)
        ok = "verification_failed" not in assessments.values()
        apply_reverification(run_id=run_id, new_generation=new_generation, supported_actions=supported,
                             verification_ok=ok, tasks=self.tasks, approvals=self.approvals,
                             journal_complete=self.publisher_complete, now=self.clock(), notifier=self.notifier)


# --------------------------------------------------------------------------------------------
# API ports
# --------------------------------------------------------------------------------------------


def _tree_commit(trees: DirectoryTreeSource) -> str:
    h = hashlib.sha256()
    for path, text in sorted(trees.files("", "").items()):
        h.update(path.encode())
        h.update(b"\0")
        h.update(text.encode())
    return "local-" + h.hexdigest()[:12]


class TaskPortAdapter:
    """The API's task port over the real execution layer."""

    def __init__(self, w: "Wiring") -> None:
        self.w = w

    # -- helpers ---------------------------------------------------------------------------------

    def _project(self, run_id: str | None) -> str | None:
        """The project of a run: stored on its context when it was admitted (never from a request)."""
        stored = self.w.orchestrator.runs.run(run_id) if run_id else None
        if stored is not None:
            if stored["ctx"].project_id:
                return stored["ctx"].project_id
            snap = self.w.snapshots.get(stored["ctx"].snapshot_id) or {}
            if snap.get("project_id"):
                return str(snap["project_id"])
        return None

    def _task_project(self, task: Any) -> str | None:
        return task.project_id or self._project(task.run_id)

    def _view(self, task_id: str) -> dict[str, Any]:
        task = self.w.tasks.get(task_id)
        decision = self.w.task_gate.revalidate(task.security_decision_id, self.w.clock()) if task.security_decision_id else None
        summary = ({"decision_id": decision.decision_id, "state": decision.state.value,
                    "valid_until": decision.valid_until.isoformat() if decision.valid_until else None,
                    "reasons": list(decision.reasons)} if decision else None)
        validation = None
        if task.published_commit:
            try:
                validation = self.w.validation.summary(task_id)
            except Exception:
                validation = None
        view = self.w.tasks.view(task_id, security_summary=summary, validation=validation)
        view["project_id"] = self._task_project(task)
        view["required_scopes"] = ["internal"]
        if not view.get("pull_request") and self.w.publisher is not None and task.destination_repo:
            try:  # the publication journal holds the pull request number once it is opened
                row = self.w.publisher._last(task.action_id, "pull_request")
            except Exception:  # noqa: BLE001 - the link is a convenience; the task view must still load
                row = None
            if row and row.get("remote_id"):
                number = int(row["remote_id"])
                view["pull_request"] = {"number": number, "status": row.get("status"),
                                        "url": f"https://github.com/{task.destination_repo}/pull/{number}"}
        manifest = self.w.target_manifest
        if manifest is not None and CHANGE_HEADING in str(task.task_text):
            view["target_label"] = manifest.label
            view["target_data_mode"] = manifest.data_mode
            view["target_change"] = change_section(task.task_text)
        return view

    def _refuse(self, exc: Exception, status: int = 409) -> Refused:
        return Refused(str(exc), status)

    # -- port ------------------------------------------------------------------------------------

    def create(self, run_id: str, generation: int, action: Mapping[str, Any], principal_role: str,
               idempotency_key: str) -> Mapping[str, Any]:
        target = self.w.publish_target
        if not target:
            raise Refused("set ATLAS_PUBLISH_TARGET to the repository the coding agent may write to", 409)
        stored = self.w.orchestrator.runs.run(run_id)
        auth = self.w.orchestrator.runs.authoritative(run_id)
        if stored is None or auth is None:
            raise Refused("the run has no verified result", 409)
        project_id = self._project(run_id) or ""
        if not self.w.projects.may_publish(project_id, target):
            raise Refused(f"the project has no export grant to publish to {target}; add it to the "
                          "project's publish_targets in the projects file", 409)
        artefact = auth[1]
        task_text = str((artefact.get("evidence_recommendation") or {}).get("text") if isinstance(artefact.get("evidence_recommendation"), Mapping)
                        else artefact.get("root_cause") or "Fix the verified cause of the failure")
        if isinstance(artefact.get("root_cause"), Mapping):
            task_text = str(artefact["root_cause"].get("text") or task_text)
        classification = str((artefact.get("classification") or {}).get("value") or "unclassified").lower()
        scope = self.w.task_scope
        if self.w.target_manifest is not None:
            causes = [task_text] + [str(c.get("text", "")) for c in artefact.get("claims", []) if isinstance(c, Mapping)]
            try:
                mapped = self.w.target_manifest.map(classification, causes)
            except NoTargetMapping as exc:
                raise Refused(str(exc), 409) from exc
            task_text, scope = mapped.task_text, mapped.scope or scope
        try:
            node_id = self.w.resolve_node_id(target)
        except Exception as exc:  # noqa: BLE001 - the task would bind a destination nobody has seen
            raise Refused(f"the target repository {target} could not be read: {exc}", 409) from exc
        try:
            task = self.w.tasks.create(
                run_id=run_id, generation=generation, action=str(action.get("value")), task_class=classification,
                task_text=task_text, scope=scope, destination_repo=target,
                destination_node_id=node_id, base_ref=self.w.base_ref, base_commit=self.w.base_commit,
                snapshot_id=stored["ctx"].snapshot_id, policy_version=POLICY_VERSION, scanner=self.w.scanner,
                gate=self.w.task_gate, now=self.w.clock(), idempotency_key=idempotency_key,
                project_id=project_id)
        except (TaskError, ValueError) as exc:
            raise self._refuse(exc) from exc
        return self._view(task.task_id)

    def list(self, project_id: str | None = None) -> list[Mapping[str, Any]]:
        tasks = self.w.tasks.all()
        return [self._view(t.task_id) for t in tasks if project_id is None or self._task_project(t) == project_id]

    def get(self, task_id: str) -> Mapping[str, Any] | None:
        try:
            return self._view(task_id)
        except KeyError:
            return None

    def _approval_details(self, task: Any, generation: int | None = None) -> dict[str, Any]:
        """What the approval screen shows: destination, base, generation, security decision and the
        frozen change (hash and text) once the task is prepared."""
        decision = (self.w.task_gate.revalidate(task.security_decision_id, self.w.clock())
                    if task.security_decision_id else None)
        artefact = dict(task.artefact or {})
        return {"destination_repository": task.destination_repo, "base_ref": task.base_ref,
                "base_commit": task.base_commit, "publication_ref": task.publication_ref,
                "verification_generation": generation if generation is not None else task.generation,
                "security_decision_id": task.security_decision_id,
                "security_state": decision.state.value if decision else None,
                "security_valid_until": decision.valid_until.isoformat() if decision and decision.valid_until else None,
                "diff_hash": artefact.get("diff_sha256") or artefact.get("public_diff_sha256"),
                "artefact_hash": artefact.get("tree_sha256") or artefact.get("artefact_sha256"),
                "diff": artefact.get("public_diff") or artefact.get("diff")}

    def _pending(self) -> list[dict[str, Any]]:
        """Approvals a person can give now (one row per task waiting for Approval A or B)."""
        rows = []
        for t in self.w.tasks.all():
            kind = ("A" if t.state in (TaskState.PROPOSED, TaskState.AWAITING_REAPPROVAL)
                    else "B" if t.state == TaskState.PREPARED else None)
            if kind:
                rows.append({"approval_id": f"pending-{kind}-{t.task_id}", "task_id": t.task_id, "kind": kind,
                             "state": "requested", "generation": t.generation, "expires_at": None,
                             "created_at": t.updated_at.isoformat(), "approver_role": None,
                             "project_id": self._task_project(t), "required_scopes": ["internal"],
                             **self._approval_details(t)})
        return rows

    def approvals(self, project_id: str | None = None) -> list[Mapping[str, Any]]:
        granted = []
        for t in self.w.tasks.all():
            for r in self.w.approvals.rows(t.task_id):
                granted.append({"approval_id": r["approval_id"], "task_id": r["task_id"], "kind": r["kind"],
                                "state": r["status"], "generation": r["generation"], "expires_at": r["expires_at"],
                                "created_at": r.get("created_at"), "approver_role": r.get("approver_role"),
                                "project_id": self._task_project(t), "required_scopes": ["internal"],
                                **self._approval_details(t, r["generation"])})
        rows = self._pending() + granted
        rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)  # newest first
        return [r for r in rows if project_id is None or r.get("project_id") == project_id]

    def approval(self, approval_id: str) -> Mapping[str, Any] | None:
        return next((a for a in self.approvals() if a["approval_id"] == approval_id), None)

    def decide(self, approval_id: str, decision: str, review_minutes: float, principal_role: str,
               idempotency_key: str) -> Mapping[str, Any]:
        row = self.approval(approval_id)
        if row is None:
            raise Refused("unknown approval", 404)
        if row["state"] != "requested":
            raise Refused(f"this approval is already {row['state']}", 409)
        task_id, kind = row["task_id"], row["kind"]
        now = self.w.clock()
        try:
            if decision != "approve":
                self.w.approvals.void_task(task_id, f"Approval {kind} rejected by a person", now)
                task = self.w.tasks.get(task_id)
                if task.state != TaskState.VOIDED:
                    self.w.tasks.transition(task_id, TaskState.VOIDED, f"Approval {kind} rejected", now)
                return {**row, "state": "rejected", "review_minutes": review_minutes}
            grant = self.w.approvals.grant_a if kind == ApprovalKind.A.value else self.w.approvals.grant_b
            aid = grant(task_id, role=principal_role, eligibility=self.w.eligibility, now=now,
                        idempotency_key=idempotency_key)
        except (ApprovalError, TaskError, ValueError) as exc:
            raise self._refuse(exc) from exc
        return {"approval_id": aid, "task_id": task_id, "kind": kind, "state": "granted",
                "review_minutes": review_minutes, "project_id": row.get("project_id"), "required_scopes": ["internal"]}

    def prepare(self, task_id: str, principal_role: str, idempotency_key: str) -> Mapping[str, Any]:
        if principal_role != "em":
            raise Refused("preparation is started by the engineering manager role", 403)
        try:
            self.w.preparer.prepare(task_id, self.w.clock())
        except PreparationRefused as exc:
            raise self._refuse(exc) from exc
        except (TaskError, ValueError, RuntimeError) as exc:
            raise self._refuse(exc) from exc
        return self._view(task_id)

    def one_click(self, task_id: str, principal_role: str, idempotency_key: str) -> Mapping[str, Any]:
        """One engineering-manager click: approval A, preparation in the sandbox, approval B, draft PR.

        Approval B is still evaluated by the eligibility gate: it is granted only when every sandbox
        check passed, so a failed check stops here and the task waits for a person.
        """
        if principal_role != "em":
            raise Refused("the one-click path is started by the engineering manager role", 403)
        if self.w.tasks.get(task_id).state in (TaskState.PROPOSED, TaskState.AWAITING_REAPPROVAL):
            self.decide(f"pending-A-{task_id}", "approve", 0.0, principal_role, f"{idempotency_key}:a")
        if self.w.tasks.get(task_id).state == TaskState.APPROVED_A:
            self.prepare(task_id, principal_role, f"{idempotency_key}:p")
        if self.w.tasks.get(task_id).state == TaskState.PREPARED:
            self.decide(f"pending-B-{task_id}", "approve", 0.0, principal_role, f"{idempotency_key}:b")
        if self.w.tasks.get(task_id).state == TaskState.APPROVED_B:
            return self.publish(task_id, principal_role, f"{idempotency_key}:u")
        return self._view(task_id)

    def publish(self, task_id: str, principal_role: str, idempotency_key: str) -> Mapping[str, Any]:
        if self.w.publisher is None:
            raise NotWired(NOT_CONNECTED_PUBLICATION)
        if principal_role != "em":
            raise Refused("publication is started by the engineering manager role", 403)
        try:
            self.w.publisher.publish(task_id, self.w.clock())
        except (PublicationRefused, PublicationStopped, PublicationUnavailable) as exc:
            raise self._refuse(exc) from exc
        except (TaskError, ValueError) as exc:
            raise self._refuse(exc) from exc
        except Exception as exc:  # noqa: BLE001 - the remote did not answer; the journal keeps the intent
            raise Refused("the target repository did not confirm the last step. Nothing is repeated blindly: "
                          f"publish again and ATLAS first reads what is already there ({type(exc).__name__})",
                          502) from exc
        if self.w.monitor is not None:
            self.w.monitor.write_scorecard(task_id, self.w.clock())
        return self._view(task_id)

    def adjudicate(self, task_id: str, verdict: str, note: str, principal_role: str,
                   idempotency_key: str) -> Mapping[str, Any]:
        now = self.w.clock()
        try:
            self.w.validation.record_adjudication(task_id, adjudicator=principal_role, correct=(verdict == "correct"),
                                                  reason=note, now=now)
            self.w.validation.apply(task_id, now)
            if self.w.monitor is not None:
                self.w.monitor.write_scorecard(task_id, now)
        except (TaskError, ValueError) as exc:
            raise self._refuse(exc) from exc
        return self._view(task_id)

    def security_refresh(self, task_id: str, principal_role: str, idempotency_key: str) -> Mapping[str, Any]:
        try:
            out = security_refresh(task_id, role=principal_role, tasks=self.w.tasks, approvals=self.w.approvals,
                                   scanner=self.w.scanner, gate=self.w.task_gate, now=self.w.clock(),
                                   idempotency_key=idempotency_key)
        except (RefreshError, TaskError, ValueError) as exc:
            raise self._refuse(exc) from exc
        return {**out, "task": self._view(task_id)}

    def scorecard(self, project_id: str | None = None) -> Mapping[str, Any]:
        owner = {t.task_id: self._task_project(t) for t in self.w.tasks.all()}
        rows = [{**dict(r), "net_contribution": dict(r).get("net_contribution", "unavailable"),
                 "data_mode": dict(r).get("data_mode", "REAL"), "range": dict(r).get("range"),
                 "project_id": owner.get(dict(r).get("task_id"))}
                for r in self.w.scorecard.latest_rows()]
        rows = [r for r in rows if project_id is None or r["project_id"] == project_id]
        return {"rows": rows, "net_contribution": "unavailable", "baseline": "SYNTHETIC",
                "data_mode": "SYNTHETIC", "required_scopes": ["internal"]}

    def tasks_for_run(self, run_id: str) -> list[Mapping[str, Any]]:
        return self.w.disposition.tasks_for_run(run_id)

    def apply_reverification(self, run_id: str, new_generation: int, assessments: Mapping[str, str]) -> None:
        self.w.disposition.apply_reverification(run_id, new_generation, assessments)


SWITCH_ROWS = {rule.trigger: row for row, rule in RULES.items()}


class ContainmentPortAdapter:
    """The API's containment port over the durable containment table."""

    def __init__(self, store: ContainmentStore, clock: Callable[[], datetime] = _now, notifier: Any = None) -> None:
        self.store = store
        self.clock = clock
        self.notifier = notifier

    def permits(self, consumer: str) -> Any:
        return self.store.permits(consumer)

    def _incident(self, incident_id: str) -> dict[str, Any]:
        out = self.store.incident(incident_id)
        info = SWITCHES[out["row"]]
        out["switch"] = out["switch_id"] = info.switch_id
        out["title"] = info.title
        out["kind"] = "Drill" if out.get("drill") else "Incident"
        detail = json.loads(out.get("detail_json") or "{}")
        out["triggered_by"] = detail.get("triggered_by")
        out["note"] = detail.get("note")
        out["blocks_in_words"] = [BLOCKS_IN_WORDS[b] for b in out.get("blocks", [])]
        out["open"] = out["status"] == "tripped"
        out["state"] = "open" if out["open"] else "resumed"
        out["trigger_event"] = out.get("trigger")
        out["recorded_prerequisites"] = [
            {"kind": p.get("kind"), "reference": json.loads(p.get("detail_json") or "{}").get("reference")}
            for p in out.get("prerequisites", [])]
        out["required_scopes"] = ["internal"]
        return out

    def state(self) -> Mapping[str, Any]:
        incidents = [self._incident(r["incident_id"]) for r in self.store.open_incidents()]
        by_row: dict[int, dict[str, Any]] = {}
        for i in incidents:  # a real incident wins over a drill when both exist for one switch
            if i["row"] not in by_row or not i["drill"]:
                by_row[i["row"]] = i
        switches = []
        for row, rule in sorted(RULES.items()):
            inc = by_row.get(row)
            switches.append({
                "switch_id": SWITCHES[row].switch_id, "switch": SWITCHES[row].switch_id,
                "title": SWITCHES[row].title, "blocks": [BLOCKS_IN_WORDS[b] for b in rule.blocks],
                "blocks_ids": list(rule.blocks),
                "state": "ok" if inc is None else ("drill" if inc["drill"] else "tripped"),
                "incident": inc, "last_tripped_at": self.store.last_tripped(row), "can_drill": inc is None})
        return {"switches": switches, "incidents": incidents}

    def incident(self, incident_id: str) -> Mapping[str, Any] | None:
        """One incident by id, open or resumed; None when there is no such incident."""
        try:
            return self._incident(incident_id)
        except (KeyError, ContainmentError):
            return None

    def switch(self, switch_id: str) -> Mapping[str, Any] | None:
        row = self._row(switch_id)
        incidents = [self._incident(r["incident_id"]) for r in self.store.open_incidents() if r["row"] == row]
        if not incidents:
            return None
        return {"switch": SWITCHES[row].switch_id, "switch_id": SWITCHES[row].switch_id,
                "title": SWITCHES[row].title, "trigger": RULES[row].trigger, "incidents": incidents}

    @staticmethod
    def _row(switch_id: str) -> int:
        if str(switch_id).isdigit() and int(switch_id) in RULES:
            return int(switch_id)
        if switch_id in ROW_BY_SWITCH_ID:
            return ROW_BY_SWITCH_ID[switch_id]
        if switch_id in SWITCH_ROWS:
            return SWITCH_ROWS[switch_id]
        raise Refused(f"unknown switch {switch_id}", 404)

    def trip(self, switch: str, reason: str) -> str:
        row = self._row(switch)
        extra = {"publication_id": "scenario-publication", "offending_item_id": "scenario-item"} if row in (8, 10) else {}
        return self.store.trip(row, producer="scenario", detail={"reason": reason}, now=self.clock(), **extra)

    def drill(self, switch_id: str, note: str, principal_role: str, user: str | None = None) -> Mapping[str, Any]:
        """Trip a switch as a drill, through the same path as a real trip, and tell people."""
        row = self._row(switch_id)
        try:
            incident_id = self.store.trip(row, producer="drill", now=self.clock(), drill=True,
                                          detail={"note": note, "triggered_by": {"role": principal_role, "user": user}})
        except ContainmentError as exc:
            raise Refused(str(exc), 409) from exc
        if self.notifier is not None:
            try:
                self.notifier.emit("containment.tripped", incident_id, [incident_id], frozenset({"internal"}),
                                   {"text": f"Drill: kill switch {SWITCHES[row].title} tripped", "drill": True,
                                    "switch_id": SWITCHES[row].switch_id, "required_scopes": ["internal"]})
            except Exception:
                pass
        return self._incident(incident_id)

    def record_prerequisite(self, incident_id: str, kind: str, reference: str, note: str,
                            principal_role: str) -> Mapping[str, Any]:
        try:
            pid = self.store.record_prerequisite(incident_id, kind, produced_by="api",
                                                 detail={"reference": reference, "note": note, "role": principal_role},
                                                 now=self.clock())
        except ContainmentError as exc:
            raise Refused(str(exc), 404 if "unknown incident" in str(exc) else 409) from exc
        return {"prerequisite_id": pid, "incident_id": incident_id, "kind": kind}

    def resume(self, incident_id: str, prerequisite_ids: Sequence[str], principal_role: str) -> Mapping[str, Any]:
        try:
            self.store.resume(incident_id, prerequisite_ids, role=principal_role, now=self.clock())
        except ContainmentError as exc:
            raise Refused(str(exc), 409) from exc
        return self._incident(incident_id)

    def authorise_correction(self, incident_id: str, body: Mapping[str, Any], principal_role: str) -> Mapping[str, Any]:
        raise NotWired(NOT_CONNECTED_CORRECTIONS)

    def corrections(self, incident_id: str) -> list[Mapping[str, Any]]:
        try:
            return list(self.store.incident(incident_id).get("corrections", []))
        except KeyError:
            return []

    def correction(self, correction_id: str) -> Mapping[str, Any] | None:
        return None

    def apply_correction(self, body: Mapping[str, Any], principal_role: str, idempotency_key: str) -> Mapping[str, Any]:
        raise NotWired(NOT_CONNECTED_CORRECTIONS)


class ContainmentEvents:
    """Turns the outbound boundary's containment events into containment trips and notifications."""

    def __init__(self, store: ContainmentStore, notifier: Any, clock: Callable[[], datetime] = _now) -> None:
        self.store = store
        self.notifier = notifier
        self.clock = clock

    def record_event(self, event_type: str, detail: Mapping[str, Any]) -> None:
        row = EVENT_ROWS.get(event_type)
        if row is None:
            return
        incident_id = self.store.trip(row, producer="outbound", detail=dict(detail), now=self.clock())
        for name in ("egress.offline_attempt",) if row == 4 else ():
            self._notify(name, incident_id)
        self._notify("containment.tripped", incident_id)

    def _notify(self, event_type: str, subject: str) -> None:
        try:
            self.notifier.emit(event_type, subject, [subject], frozenset({"internal"}),
                               {"text": f"{event_type}: {subject}", "required_scopes": ["internal"]})
        except Exception:
            pass


# --------------------------------------------------------------------------------------------
# connect
# --------------------------------------------------------------------------------------------


@dataclass
class Wiring:
    """Everything ``connect`` built, kept for tests and for the smoke checks."""

    settings: Any
    clock: Callable[[], datetime]
    ops: OpsDB
    spend: SpendMeter
    containment: ContainmentStore
    outbound: OutboundBoundary
    store: Any
    snapshots: Any
    security_partition: ActionSecurityPartition
    scanner: Any
    task_gate: TaskSecurityGate
    tasks: TaskStore
    approvals: ApprovalService
    eligibility: Eligibility
    runner: Any
    preparer: Preparer
    validation: ValidationService
    scorecard: Scorecard
    orchestrator: Orchestrator
    disposition: ExecutionDisposition
    publish_target: str | None
    base_ref: str
    base_commit: str
    task_scope: tuple[str, ...]
    default_project: str | None
    projects: Any = None
    data_layer: str = "not connected"
    target_manifest: Any = None
    publisher: Any = None
    monitor: Any = None
    resolve_node_id: Callable[[str], str] = lambda target: "unresolved"
    extra: dict[str, Any] = field(default_factory=dict)


def _env_values(settings: Any, env_file: str | Path | None) -> dict[str, str]:
    env: dict[str, str] = {}
    if env_file and Path(env_file).exists():
        env.update(parse_env_file(Path(env_file)))
    env.update({k: v for k, v in os.environ.items() if k.startswith(("ATLAS_", "ANTHROPIC_"))})
    return env


def connect(services: Any, settings: Any, *, env: Mapping[str, str] | None = None, provider: Any = None,
            scanner: Any = None, runner: Any = None, clock: Callable[[], datetime] | None = None,
            persist: bool = True, env_file: str | Path | None = ".env", data_transport: Any = None,
            publication_api: Any = None) -> Wiring:
    """Build the real layers and plug them into ``services`` (the API container)."""
    clock = clock or _now
    env = dict(env if env is not None else _env_values(settings, env_file))
    data_dir = Path(settings.data_dir) / "local" if persist else None
    environment = getattr(services, "environment", "normal")

    def db_path(name: str) -> str:
        if data_dir is None:
            return ":memory:"
        data_dir.mkdir(parents=True, exist_ok=True)
        return str(data_dir / f"{environment}-{name}.sqlite")

    notifier = services.notifier
    ops = OpsDB(db_path("ops"))
    spend = SpendMeter(float(settings.spend_ceiling_usd),
                       notify=lambda event, amount: _safe_emit(notifier, event, "spend",
                                                               f"model spend has reached {amount:.2f} USD"),
                       path=(data_dir / f"{environment}-spend.json") if data_dir is not None else None)
    containment = ContainmentStore(ops, deployed_revision=lambda: env.get("ATLAS_DEPLOYED_REVISION", "local"))
    if provider is None:
        try:
            provider = provider_from_env(env)
        except ValueError:
            provider = None  # the boundary refuses model calls with a clear reason
    outbound = OutboundBoundary(mode=services.mode.get, containment=CeilingGate(spend, containment),
                                events=ContainmentEvents(containment, notifier, clock), provider=provider,
                                internal_model_hosts=frozenset(settings.model_internal_hosts or ()), spend=spend.add)

    default_project = getattr(services, "default_project_id", None) or (settings.default_project or None)
    if getattr(services, "feed", None) is None:
        services.feed = ProjectFeed(services.hub)
    target_dir = Path(env.get("ATLAS_TARGET_DIR") or DEFAULT_TARGET_DIR)
    data = None
    if data_dir is not None:
        events = ContainmentEvents(containment, notifier, clock)
        data = connect_data(settings, env, mode=services.mode.get, feed=services.feed,
                            on_violation=lambda kind, detail: events.record_event(GATEWAY_EVENTS.get(kind, kind), detail),
                            transport=data_transport, target_dir=target_dir, clock=clock,
                            default_project=default_project)
        store = data.store
        snapshots = data.snapshots
        partition = data.security_reader
        scanner = scanner or data.scanner
        components = data.scanner.components
    else:
        store = EmptyEvidenceStore()
        snapshots = LocalSnapshots(None, default_project, clock)
        partition = ActionSecurityPartition()
        scanner = scanner or UnconnectedScanner()
        components = lambda ctx: ["target"]  # noqa: E731
    task_gate = TaskSecurityGate(partition, components=components, clock=clock)
    tasks = TaskStore(ops)
    approvals = ApprovalService(ops, tasks, notifier, containment)
    runs = RunStore(db_path("runs"))
    run_state = RunStateAdapter(runs)
    eligibility = Eligibility(tasks, approvals, task_gate, run_state, containment)
    runner = runner or select_runner(env)
    trees = DirectoryTreeSource(target_dir)
    coding = CodingAgent(MessageOutbound(outbound, DIFF_SCHEMA))
    reviewer = SentinelDiffReviewer(MessageOutbound(outbound, REVIEW_SCHEMA))
    preparer = Preparer(tasks, eligibility, coding, runner, reviewer, services.projection, trees, containment)
    # Validation evidence lives in the data layer's action store; memory only when there is no data layer.
    evidence = StoreValidationEvidence(data.action_store, clock) if data is not None else ValidationEvidenceMemory()
    validation = ValidationService(tasks, evidence)
    scorecard = Scorecard(ops, notifier)
    publisher_box: dict[str, Any] = {}
    disposition = ExecutionDisposition(
        tasks, approvals,
        lambda action_id: bool(publisher_box.get("publisher") and publisher_box["publisher"].complete(action_id)),
        notifier, clock)
    gate = RunSecurityGate(POLICY_VERSION, clock)
    orchestrator = Orchestrator(Deps(store=store, outbound=outbound, gate=gate, queue=services.queue,
                                     notifier=notifier, events=services.events, approvals=approvals,
                                     tasks=disposition,
                                     content_of=data.store.content_of if data else (lambda r: r.content_ref),
                                     open_items_for=data.open_items.open_items_for if data else (lambda ctx: []),
                                     components_for=(data.components_for if data else (lambda trigger: trigger.get("components", []))),
                                     use_classifier=(env.get("ATLAS_INJECTION_CLASSIFIER") or "on").lower() != "off",
                                     clock=clock), runs=runs)

    target = settings.publish_target if settings.publish_target and "<" not in settings.publish_target else None
    publisher = None
    manifest = TargetManifest.load(target_dir.parent / "demo_target.toml")
    monitor = None
    resolve_node_id: Callable[[str], str] = lambda _target: "unresolved"  # noqa: E731
    if data is not None and target and (publication_api is not None or bool(getattr(settings, "publish_token", ""))):
        def grant_for(full_name: str) -> PublishGrant | None:
            """The publish grant of the one task approved to write to this repository, if any."""
            for t in tasks.all():
                if t.destination_repo.lower() != full_name.lower() or t.state != TaskState.APPROVED_B:
                    continue
                row = approvals.current(t.task_id, ApprovalKind.B)
                if row is None:
                    continue
                return PublishGrant(t.destination_repo, t.publication_ref, t.base_ref,
                                    datetime.fromisoformat(str(row["expires_at"])))
            return None

        registry = getattr(services, "projects", None) or ProjectRegistry()
        api = publication_api or GitHubPublicationAPI(data.gateway, grant_for)
        publisher = Publisher(ops, tasks, approvals, eligibility, api,
                              services.projection, preparer.base_tree, containment,
                              publish_grant=registry.may_publish,
                              upstream_repositories=data.gateway.sources.repositories(),
                              notifier=notifier, not_compared=frozenset(manifest.not_compared) if manifest else frozenset())
        publisher_box["publisher"] = publisher
        base_ref = env.get("ATLAS_PUBLISH_BASE_REF", "main")

        def resolve_node_id(full_name: str) -> str:
            """The identity of the target, read once when a task is created, so approvals bind it."""
            if hasattr(api, "identify"):
                return api.identify(full_name, base_ref, clock()).node_id
            return api.repo_identity(full_name).node_id

        from atlas.connectors.target_ci import TargetCIAdapter
        from atlas.gateway import PublicationRecord

        monitor = TargetMonitor(
            tasks, validation, TargetCIAdapter(data.gateway, data.action_store, clock=clock),
            lambda t: PublicationRecord(t.destination_repo, t.action_id, t.published_commit, t.pr_number, t.state),
            scorecard=scorecard, approvals=approvals, clock=clock)
    w = Wiring(settings=settings, clock=clock, ops=ops, spend=spend, containment=containment, outbound=outbound,
               store=store, snapshots=snapshots, security_partition=partition, scanner=scanner, task_gate=task_gate,
               tasks=tasks, approvals=approvals, eligibility=eligibility, runner=runner, preparer=preparer,
               validation=validation, scorecard=scorecard, orchestrator=orchestrator, disposition=disposition,
               publish_target=target, base_ref=env.get("ATLAS_PUBLISH_BASE_REF", "main"),
               base_commit=_tree_commit(trees) if target_dir.exists() else "unknown",
               task_scope=tuple(s for s in (env.get("ATLAS_TASK_SCOPE") or "*.go").split(",") if s),
               target_manifest=manifest, publisher=publisher, monitor=monitor, resolve_node_id=resolve_node_id,
               default_project=default_project, projects=getattr(services, "projects", None) or ProjectRegistry(),
               data_layer="connected" if data else "not connected")
    w.extra["data"] = data

    # Export grants for non-person principals come from the projects file.
    inner = getattr(notifier, "inner", notifier)
    if hasattr(inner, "export_grant"):
        inner.export_grant = w.projects.may_notify
    services.orchestrator = orchestrator
    services.snapshots = snapshots
    services.tasks = TaskPortAdapter(w)
    services.containment = ContainmentPortAdapter(containment, clock, notifier)
    services.spend = spend
    services.model_configured = provider is not None and bool(getattr(settings, "anthropic_api_key", "") or
                                                               settings.model_provider != "anthropic")
    staged = () if publisher is not None else ("publication (set ATLAS_PUBLISH_TOKEN and a publish target to connect it)",)
    staged = staged + ("corrections (" + NOT_CONNECTED_CORRECTIONS + ")",)
    if data is None:
        staged = ("evidence data (" + NOT_CONNECTED_DATA + ")",) + staged
    services.staged = tuple(services.staged) + staged
    variables = dict(getattr(services, "demo_variables", {}) or {})
    if getattr(settings, "peer_ui_url", ""):
        variables.setdefault("peer_ui_url", settings.peer_ui_url)
    if getattr(settings, "scenario_url", ""):
        variables.setdefault("scenario_url", settings.scenario_url)
    if getattr(settings, "base_url", ""):
        variables.setdefault("base_url", settings.base_url)
    services.demo_variables = variables
    services.wiring = w
    # Automatic runs: the scheduler loop (started by the server) polls these; nothing starts here.
    from atlas.watchers import register_watchers

    w.extra["watchers"] = register_watchers(services, env=env, nightly_time=getattr(settings, "nightly_time", "02:00"),
                                            clock=clock)
    return w


def _safe_emit(notifier: Any, event: str, subject: str, text: str) -> None:
    try:
        notifier.emit(event, subject, [subject], frozenset({"internal"}), {"text": text, "required_scopes": ["internal"]})
    except Exception:
        pass
