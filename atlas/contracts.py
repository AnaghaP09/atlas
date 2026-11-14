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
    "project.activity",
    "project.dashboard_changed",
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
    project_id: str = ""


@dataclass(frozen=True)
class ValidationContext:
    """Read access to post-publication evidence for one published action."""

    destination_repo: str
    action_id: str
    published_commit: str
    project_id: str = ""


@dataclass(frozen=True)
class SecurityContext:
    """Read access to the action-security partition for one task's scanned revision."""

    destination_repo: str
    action_id: str
    scanned_revision: str
    project_id: str = ""


@dataclass(frozen=True)
class StartedBy:
    kind: StartedByKind
    role: Role | None = None
    watcher_id: str | None = None


@dataclass(frozen=True)
class Principal:
    """Who is reading, in which project.

    ``role`` is the caller's role in ``project_id``; it is None only for the public publication
    principal. ``user`` names the signed-in person (None for the demo role header and for the
    public principal). Objects that carry a ``project_id`` are visible only inside that project.
    """

    role: Role | None
    scopes: frozenset[str]
    user: str | None = None
    project_id: str | None = None

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

    def get_record(self, ctx: RunContext, record_id: str) -> Record | None:
        """One record by its id, only if it is visible in the run's snapshot."""

    def read_content(self, ctx: RunContext, record: Record) -> str:
        """The content behind ``record.content_ref``, only if the record is visible to ``ctx``."""

    def history(self, ctx: RunContext, source: str, source_id: str) -> list[Record]:
        """All revisions visible in the run's snapshot, oldest first."""

    def search(self, ctx: RunContext, query: Mapping[str, Any]) -> list[Record]:
        """Search within the run's snapshot only."""


# ---------------------------------------------------------------------------
# Security scan records
# ---------------------------------------------------------------------------

SECURITY_RECORD_KINDS = ("scan_run", "finding")
"""Content kinds of security records; JSON shape in ``schema/security_record.schema.json``."""


@runtime_checkable
class SecurityReader(Protocol):
    """Read access to one task's action-security partition (provided by the evidence store)."""

    def read(self, ctx: SecurityContext) -> list[Record]: ...


# ---------------------------------------------------------------------------
# Forecast input
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OpenItem:
    """One open issue at a release close, as the forecast service sees it."""

    issue_id: str
    title: str
    labels: frozenset[str]
    created: datetime
    last_activity: datetime
    component: str | None
    record_ids: tuple[str, ...]
    ci_recurrences: int = 0
    component_churn: int = 0
    fan_in: int = 0
    requirement_bound: bool = False
    required_scopes: frozenset[str] = field(default_factory=lambda: frozenset({"public"}))


@runtime_checkable
class OpenItemsSource(Protocol):
    def open_items_for(self, ctx: RunContext) -> list[OpenItem]:
        """Open items visible in the run's snapshot at its cutoff."""


# ---------------------------------------------------------------------------
# Re-verification hook (execution side)
# ---------------------------------------------------------------------------

REVERIFICATION_ASSESSMENTS = ("still_supported", "unsupported", "verification_failed")


@runtime_checkable
class TaskDisposition(Protocol):
    """Execution-layer hooks used by re-verification."""

    def tasks_for_run(self, run_id: str) -> list[Mapping[str, Any]]:
        """Tasks bound to the run: ``{"task_id", "action", "state"}``."""

    def apply_reverification(self, run_id: str, new_generation: int, assessments: Mapping[str, str]) -> None:
        """Apply one of ``REVERIFICATION_ASSESSMENTS`` per task, by publication phase."""


# ---------------------------------------------------------------------------
# Swap point 1: model provider (used only inside the outbound boundary)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelLimits:
    max_tokens: int = 4096
    effort: str | None = None


@dataclass(frozen=True)
class ModelResult:
    """Provider-neutral result of one model call."""

    content: Any  # parsed structured output (dict or list) or text
    stop: str  # complete | refused | truncated | failed
    input_tokens: int
    output_tokens: int
    model: str
    provider: str


class ModelProvider(Protocol):
    """One model backend. Used only by the outbound boundary, after redaction and every check."""

    name: str
    internal: bool  # True when the endpoint is inside the organisation's network

    def model_for(self, role: str) -> str: ...

    def endpoint_host(self) -> str | None: ...

    def complete(self, role: str, messages: Sequence[Mapping[str, Any]], schema: Mapping[str, Any] | None,
                 limits: ModelLimits) -> ModelResult: ...


# ---------------------------------------------------------------------------
# Swap point 2: identity provider
# ---------------------------------------------------------------------------


class IdentityMode(StrEnum):
    HEADER = "header"  # tests and development only, loopback bind
    LOCAL = "local"  # built-in sign-in
    OIDC = "oidc"  # company single sign-on (not available in this build)


@runtime_checkable
class IdentityProvider(Protocol):
    mode: IdentityMode

    def authenticate(self, request: Any) -> Principal | None:
        """The principal for a request, or None when it is not signed in."""

    def login(self, request: Any) -> Any:
        """Start or complete sign-in; returns the response to send."""

    def logout(self, request: Any) -> Any:
        """End the session server-side; returns the response to send."""


# ---------------------------------------------------------------------------
# Swap point 3: sandbox runner
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SandboxLimits:
    timeout_seconds: float = 300
    memory: str = "1g"
    cpus: str = "2"
    pids: int = 256


@dataclass(frozen=True)
class SandboxBundle:
    """What runs in the sandbox: the tree of the frozen artefact and where behavioural tests go."""

    tree: Mapping[str, str]
    behaviour_package: str = "netaddr"


@dataclass(frozen=True)
class IsolationSpec:
    network: str
    read_only_root: bool
    non_root: bool
    cap_drop_all: bool
    no_new_privileges: bool
    pids_limit: int
    memory_bytes: int
    secrets: tuple[str, ...] = ()
    host_mounts: tuple[str, ...] = field(default_factory=tuple)


@dataclass
class SandboxResult:
    checks: dict[str, str]
    log: str = ""
    container: bool = False
    isolation: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class SandboxRunner(Protocol):
    name: str
    data_mode: str

    def prepare(self, task: Any, bundle: SandboxBundle, limits: SandboxLimits) -> SandboxResult:
        """Run the task's preparation checks on the bundle under the isolation contract."""

    def run_checks(self, bundle: SandboxBundle, limits: SandboxLimits) -> SandboxResult: ...


# ---------------------------------------------------------------------------
# Execution ports (implemented by other components, used by the execution layer)
# ---------------------------------------------------------------------------


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
