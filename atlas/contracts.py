"""Shared types and interfaces used by every part of the backend.

These definitions are the boundary between components: the evidence store, connectors, agents,
services, execution, the HTTP API and the UI all exchange data in these shapes. JSON schemas for
the same shapes live in ``schema/``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class Mode(StrEnum):
    CONNECTED = "connected"
    OFFLINE = "offline"


class RunMode(StrEnum):
    LIVE = "live"
    REPLAY = "replay"


class Provenance(StrEnum):
    OBSERVED = "observed"
    SYNTHETIC = "synthetic"
    DERIVED = "derived"


class DataMode(StrEnum):
    REAL = "REAL"
    SYNTHETIC = "SYNTHETIC"
    STAGED = "STAGED"
    UNEVALUATED = "UNEVALUATED"


class Verdict(StrEnum):
    SUPPORTED = "supported"
    REJECTED = "rejected"
    CONTRADICTED = "contradicted"
    MISSING = "missing"


class Classification(StrEnum):
    DEPENDENCY = "DEPENDENCY"
    CODE_DEFECT = "CODE_DEFECT"
    TEST_DEFECT = "TEST_DEFECT"
    ENVIRONMENT = "ENVIRONMENT"
    DUPLICATE = "DUPLICATE"
    CARRY_OVER_FORECAST = "CARRY_OVER_FORECAST"
    UNKNOWN = "UNKNOWN"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"


class Action(StrEnum):
    DELEGATE_TO_CODING_AGENT = "DELEGATE_TO_CODING_AGENT"
    ASSIGN_DEVELOPER = "ASSIGN_DEVELOPER"
    ESCALATE_DECISION = "ESCALATE_DECISION"
    HAND_TO_INFRA_OWNER = "HAND_TO_INFRA_OWNER"
    LINK_DUPLICATE = "LINK_DUPLICATE"
    PUBLISH_FORECAST = "PUBLISH_FORECAST"
    REJECT = "REJECT"


class SafetyDisposition(StrEnum):
    NONE = "NONE"
    HOLD_FOR_HUMAN = "HOLD_FOR_HUMAN"
    REJECTED = "REJECTED"


class SecurityState(StrEnum):
    NOT_RUN = "not_run"
    PASSED = "passed"
    BLOCKED = "blocked"
    ACCEPTED_WITH_EXCEPTION = "accepted_with_exception"


class RunStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNVERIFIED = "unverified"
    FAILED = "failed"


class StartedByKind(StrEnum):
    SCHEDULE = "schedule"
    WATCHER = "watcher"
    PERSON = "person"


class Role(StrEnum):
    PM = "pm"
    EM = "em"
    ARCHITECT = "architect"
    SECURITY = "security"
    ONCALL = "oncall"
    AUDIT = "audit"


PUBLIC_PRINCIPAL = "public"
"""Principal used for anything published outside the backend (anonymous readers)."""


class TaskState(StrEnum):
    PROPOSED = "proposed"
    APPROVED_A = "approved_a"
    PREPARED = "prepared"
    APPROVED_B = "approved_b"
    PUBLISHED = "published"
    AWAITING_VALIDATION = "awaiting_validation"
    ACCEPTED = "accepted"
    FAILED = "failed"
    VOIDED = "voided"
    AWAITING_REAPPROVAL = "awaiting_reapproval"


UNPUBLISHED_STATES = frozenset(
    {TaskState.PROPOSED, TaskState.APPROVED_A, TaskState.PREPARED, TaskState.APPROVED_B}
)
PUBLISHED_STATES = frozenset(
    {TaskState.PUBLISHED, TaskState.AWAITING_VALIDATION, TaskState.ACCEPTED, TaskState.FAILED}
)


class SupportStatus(StrEnum):
    SUPPORTED = "supported"
    WITHDRAWN = "withdrawn"


class ApprovalKind(StrEnum):
    A = "A"
    B = "B"


class EgressPurpose(StrEnum):
    MODEL_CALL = "model_call"
    LIVE_INGEST = "live_ingest"
    ACTION_SECURITY_INGEST = "action_security_ingest"
    PUBLICATION = "publication"
    TARGET_CI_READ = "target_ci_read"
    WEBHOOK_DELIVERY = "webhook_delivery"
    CORRECTION_PREAUTH_READ = "correction_preauth_read"
    CORRECTION_WRITE = "correction_write"
    REMEDIATION_VERIFY_READ = "remediation_verify_read"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    HIGH = "high"
    CRITICAL = "critical"


class NotificationKind(StrEnum):
    ALERT = "alert"
    REMINDER = "reminder"
    DECISION = "decision"


EVENT_NAMES: tuple[str, ...] = (
    "run.progress",
    "run.status_changed",
    "run.generation_changed",
    "report.superseded",
    "report.corrected",
    "report.withdrawn",
    "task.state_changed",
    "approval.voided",
    "notification.created",
    "containment.tripped",
    "containment.resumed",
    "stream.reset",
)
"""Live event names; must match ``schema/events.json`` exactly."""


# ---------------------------------------------------------------------------
# Contexts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunContext:
    """Immutable binding of a run to one snapshot. Every evidence read takes it."""

    run_id: str
    snapshot_id: str
    mode: RunMode
    release_or_case: str
    cutoff: datetime
    alias_variant: str | None
    evidence_clock: datetime


@dataclass(frozen=True)
class ValidationContext:
    """Read access to post-publication evidence for one published action."""

    destination_repo: str
    action_id: str
    published_commit: str


@dataclass(frozen=True)
class SecurityContext:
    """Read access to the action-security partition for one task's scanned revision."""

    destination_repo: str
    action_id: str
    scanned_revision: str


@dataclass(frozen=True)
class StartedBy:
    kind: StartedByKind
    role: Role | None = None
    watcher_id: str | None = None


@dataclass(frozen=True)
class Principal:
    """Who is reading. ``role`` is None only for the public publication principal."""

    role: Role | None
    scopes: frozenset[str]

    @property
    def is_public(self) -> bool:
        return self.role is None


# ---------------------------------------------------------------------------
# Records and claims
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Record:
    record_id: str
    source: str
    source_id: str
    revision: int | str | None
    content_version_id: str
    valid_from: datetime
    observed_at: datetime
    confirmed_through: datetime | None
    writer: str
    provenance: Provenance
    synthetic_ancestry: bool
    access_scopes: frozenset[str]
    lineage: tuple[str, ...]
    content_ref: str
    fetched_at: datetime | None = None
    conflicts: tuple[str, ...] = ()
    superseded_by: str | None = None


@dataclass(frozen=True)
class Claim:
    id: str
    text: str
    kind: str
    evidence_ids: tuple[str, ...]
    depends_on: tuple[str, ...]
    required_scopes: frozenset[str]
    verdict: Verdict
    verdict_reason: str | None = None


@dataclass(frozen=True)
class ModelResponse:
    content: Any
    stop_reason: str
    input_tokens: int
    output_tokens: int
    model: str


@dataclass(frozen=True)
class EligibilityResult:
    allowed: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Interfaces
# ---------------------------------------------------------------------------


@runtime_checkable
class Connector(Protocol):
    name: str

    def fetch(self, query: Mapping[str, Any], ctx: RunContext) -> list[Record]:
        """Fetch records for a query. In replay mode reads frozen data only."""


@runtime_checkable
class Store(Protocol):
    def put(self, record: Record, writer: str) -> None:
        """Write one record; refused unless ``writer`` is the source's authorised writer."""

    def get(self, ctx: RunContext, source: str, source_id: str) -> Record | None:
        """Newest record visible in the run's snapshot, with conflicts attached."""

    def history(self, ctx: RunContext, source: str, source_id: str) -> list[Record]:
        """All revisions visible in the run's snapshot, oldest first."""

    def search(self, ctx: RunContext, query: Mapping[str, Any]) -> list[Record]:
        """Search within the run's snapshot only."""


@runtime_checkable
class Outbound(Protocol):
    def call(self, role: str, purpose: EgressPurpose, payload: Mapping[str, Any]) -> ModelResponse:
        """The only path to a model: redacts, checks mode and containment, counts attempts."""


@runtime_checkable
class Tool(Protocol):
    name: str
    input_schema: Mapping[str, Any]

    def run(self, ctx: RunContext, arguments: Mapping[str, Any]) -> Any:
        """Read-only tool over the run's snapshot."""


@runtime_checkable
class Approvals(Protocol):
    def void_superseded(self, run_id: str, new_generation: int) -> list[str]:
        """Void every approval for ``run_id`` bound to a generation lower than ``new_generation``.

        Returns the ids of the approvals voided. Task disposition is decided by the caller.
        Idempotent: calling twice voids nothing new.
        """


@runtime_checkable
class Projection(Protocol):
    def project(self, artifact: Mapping[str, Any], principal: Principal) -> dict[str, Any]:
        """Return the artefact with every field the principal may not see removed."""


@runtime_checkable
class Notifier(Protocol):
    def emit(
        self,
        event_type: str,
        subject_id: str,
        payload_refs: Sequence[str],
        required_scopes: frozenset[str],
    ) -> str:
        """Record a notification (severity and roles come from the catalog); returns its id."""


@runtime_checkable
class Containment(Protocol):
    def permits(self, consumer: str) -> EligibilityResult:
        """Whether a consumer (model_calls, preparation, publication, report_delivery) may act."""


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime: ...
