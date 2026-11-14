"""The fail-closed eligibility predicate and the mandatory-check manifest.

One predicate is evaluated before Approval A, before preparation, before recording Approval B and
before every publication step. It re-evaluates the security decision against the current clock,
the verification generation, injection and containment state, the approvals' bindings and every
check the manifest lists for the transition. Anything not explicitly ``passed`` fails.
"""

from __future__ import annotations

from datetime import datetime
from typing import Mapping

from atlas.approvals import ApprovalService
from atlas.containment import EXECUTION, PREPARATION, PUBLICATION
from atlas.contracts import Action, ApprovalKind, EligibilityResult, SecurityState
from atlas.execution.ports import RunState, SecurityGate
from atlas.tasks import TaskStore

MANIFEST_VERSION = "1"

APPROVAL_B_CHECKS: tuple[str, ...] = (
    "container_sandbox",
    "scope",
    "binary",
    "config_paths",
    "secret_scan",
    "personal_data_scan",
    "verifier_review",
    "go_build",
    "go_vet",
    "behavioural_tests",
)

MANIFEST: dict[str, dict] = {
    "approval_a": {"approvals": (), "checks": (), "consumers": (PREPARATION,)},
    "preparation": {"approvals": (ApprovalKind.A,), "checks": (), "consumers": (PREPARATION, EXECUTION)},
    "approval_b": {"approvals": (ApprovalKind.A,), "checks": APPROVAL_B_CHECKS, "consumers": (PREPARATION,)},
    "publication_step": {
        "approvals": (ApprovalKind.A, ApprovalKind.B),
        "checks": APPROVAL_B_CHECKS + ("outgoing_commit_match",),
        "consumers": (PUBLICATION,),
    },
}


class Eligibility:
    def __init__(self, tasks: TaskStore, approvals: ApprovalService, gate: SecurityGate, run_state: RunState,
                 containment) -> None:
        self.tasks = tasks
        self.approvals = approvals
        self.gate = gate
        self.run_state = run_state
        self.containment = containment

    def evaluate(self, task_id: str, transition: str, now: datetime,
                 extra_checks: Mapping[str, str] | None = None) -> EligibilityResult:
        if transition not in MANIFEST:
            raise ValueError(f"unknown transition {transition}")
        spec = MANIFEST[transition]
        task = self.tasks.get(task_id)
        reasons: list[str] = []

        # Executable action only (or a recorded human override to it).
        if task.executable_action != Action.DELEGATE_TO_CODING_AGENT.value:
            reasons.append(f"action {task.executable_action} is not executable")

        # Security decision re-evaluated now; the cached state is never authorisation.
        if not task.security_decision_id:
            reasons.append("no security decision")
        else:
            decision = self.gate.revalidate(task.security_decision_id, now)
            ok_state = decision.state == SecurityState.PASSED or (
                decision.state == SecurityState.ACCEPTED_WITH_EXCEPTION and decision.signoff_valid
            )
            if not ok_state:
                reasons.append(f"security decision is {decision.state.value}" + (
                    f" ({'; '.join(decision.reasons)})" if decision.reasons else ""))
            if decision.valid_until is None or decision.valid_until <= now:
                reasons.append("security decision expired")
            if decision.policy_version != task.policy_version:
                reasons.append("security policy version changed")

        # Verification generation is current and still contains the action.
        current = self.run_state.authoritative_generation(task.run_id)
        if task.generation != current:
            reasons.append(f"task bound to generation {task.generation}, authoritative is {current}")
        elif not task.override and task.action not in self.run_state.verified_actions(task.run_id, current):
            reasons.append("the verified artefact no longer contains the action")

        if self.run_state.injection_flagged(task.run_id):
            reasons.append("run is flagged for injection")

        for consumer in spec["consumers"]:
            permit = self.containment.permits(consumer)
            if not permit.allowed:
                reasons.extend(permit.reasons)

        for kind in spec["approvals"]:
            reasons.extend(self.approvals.problems(task, kind, now))

        results = dict(task.checks)
        results.update(extra_checks or {})
        for check in spec["checks"]:
            status = results.get(check, "missing")
            if status != "passed":
                reasons.append(f"check {check}: {status}")

        return EligibilityResult(not reasons, tuple(reasons))
