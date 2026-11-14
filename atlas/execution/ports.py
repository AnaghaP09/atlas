"""Interfaces the execution layer needs from other components.

The execution layer never reaches past these: security scans come from the security connector,
gate decisions from the security gate, the authoritative verification generation from the run
service, diff reviews from the verifier, remote writes from the publication gateway and
post-publication evidence from the validation store.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from atlas.contracts import SecurityContext, SecurityState, ValidationContext


@dataclass(frozen=True)
class SecurityDecision:
    decision_id: str
    state: SecurityState
    valid_until: datetime | None
    policy_version: str
    scanned_revision: str
    signoff_valid: bool = False
    reasons: tuple[str, ...] = ()


@runtime_checkable
class SecurityScanner(Protocol):
    def scan(self, ctx: SecurityContext) -> str:
        """Scan the context's revision into the action-security partition; returns the scan-run id."""


@runtime_checkable
class SecurityGate(Protocol):
    def decide(self, ctx: SecurityContext, now: datetime) -> SecurityDecision:
        """Create a new decision from the scans in the action-security partition."""

    def revalidate(self, decision_id: str, now: datetime) -> SecurityDecision:
        """The decision as it stands now: expiry, exception revocation, sign-off and policy re-checked."""


@runtime_checkable
class RunState(Protocol):
    def authoritative_generation(self, run_id: str) -> int:
        """Current authoritative verification generation of the run."""

    def verified_actions(self, run_id: str, generation: int) -> frozenset[str]:
        """Actions present in the verified artefact of that generation."""

    def injection_flagged(self, run_id: str) -> bool: ...


@dataclass(frozen=True)
class ReviewResult:
    passed: bool
    reason: str


@runtime_checkable
class DiffReviewer(Protocol):
    def review(self, task_text: str, scope: Sequence[str], diff: str) -> ReviewResult:
        """Independent review of a diff against its task (the verifier agent)."""


@dataclass(frozen=True)
class RepoIdentity:
    node_id: str
    full_name: str


@dataclass
class PullRequest:
    number: int
    head: str
    base: str
    title: str
    body: str
    draft: bool = True
    state: str = "open"


@runtime_checkable
class PublicationAPI(Protocol):
    """Remote repository operations, performed by the gateway with the scoped publication token."""

    def repo_identity(self, full_name: str) -> RepoIdentity: ...

    def branch_head(self, full_name: str, branch: str) -> str | None: ...

    def push_branch(
        self, full_name: str, branch: str, base_commit: str, files: Mapping[str, str], message: str
    ) -> str:
        """Create ``branch`` with one commit on ``base_commit`` writing ``files``; returns the commit."""

    def find_pulls(self, full_name: str, head: str) -> list[PullRequest]:
        """Pull requests with this head branch, including closed ones."""

    def create_draft_pull(self, full_name: str, head: str, base: str, title: str, body: str) -> PullRequest: ...

    def get_pull(self, full_name: str, number: int) -> PullRequest: ...

    def replace_pull_body(self, full_name: str, number: int, body: str) -> None: ...


@dataclass(frozen=True)
class EvidenceRecord:
    kind: str  # ci_observation | behavioural_check | adjudication
    data: Mapping[str, Any]
    recorded_at: datetime


@runtime_checkable
class ValidationEvidence(Protocol):
    """Action-scoped validation store: evidence only, never task state."""

    def read(self, ctx: ValidationContext, kind: str) -> list[EvidenceRecord]: ...

    def write(self, ctx: ValidationContext, kind: str, data: Mapping[str, Any], writer: str) -> None: ...
