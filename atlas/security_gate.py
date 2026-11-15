"""Security gate service.

Runs on every investigation and release close (whether or not the investigator called the scanner
tool), for every task when it is created, and again on a security refresh. ``passed`` requires a
successful, fresh scan-run record covering every component in scope with no blocking finding; the
absence of findings alone never counts as clean. Every result binds its scan runs, exceptions and
policy version and carries ``valid_until``; ``revalidate`` re-checks it against the current clock
before every execution step.
"""

from __future__ import annotations

from atlas.contracts import SecurityReader  # noqa: F401

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from atlas.contracts import Record, SecurityContext, SecurityState

SCAN_FRESHNESS = timedelta(hours=24)
BLOCKING_SEVERITIES = {"high", "critical"}


@dataclass(frozen=True)
class ScanRun:
    scan_run_id: str
    observed_at: datetime
    coverage: frozenset[str]
    status: str  # success | partial_failure | error
    finding_ids: tuple[str, ...] = ()
    target_revision: str | None = None


@dataclass(frozen=True)
class Finding:
    finding_id: str
    component: str
    severity: str | None
    fix_version: str | None
    record_id: str | None = None
    module: str | None = None
    version: str | None = None
    advisory: str | None = None
    summary: str | None = None


@dataclass(frozen=True)
class SecurityException:
    exception_id: str
    finding_id: str
    approver: str | None
    reason: str | None
    expires: datetime | None
    revoked: bool = False


@dataclass(frozen=True)
class GateResult:
    decision_id: str
    state: SecurityState
    findings: tuple[Mapping[str, Any], ...]
    scan_run_ids: tuple[str, ...]
    exception_ids: tuple[str, ...]
    policy_version: str
    valid_until: datetime | None
    signoff_required: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)

    def as_security_check(self) -> dict[str, Any]:
        return {
            "state": str(self.state),
            "findings": [dict(f) for f in self.findings],
            "policy_version": self.policy_version,
            "exception_ref": self.exception_ids[0] if self.exception_ids else None,
            "signoff_required": self.signoff_required,
            "valid_until": self.valid_until.isoformat() if self.valid_until else None,
            "scan_run_ids": list(self.scan_run_ids),
        }


def blocks(f: Finding) -> bool:
    if f.severity is None or f.severity == "":
        return True  # missing severity counts as blocking
    sev = f.severity.lower()
    if sev == "critical":
        return True
    return sev in BLOCKING_SEVERITIES and bool(f.fix_version)


def exception_valid(e: SecurityException | None, finding_id: str, now: datetime) -> bool:
    return bool(
        e and not e.revoked and e.finding_id == finding_id and e.approver and e.reason
        and e.expires is not None and e.expires > now
    )


def evaluate(
    components: Iterable[str],
    scans: Sequence[ScanRun],
    findings: Sequence[Finding],
    exceptions: Sequence[SecurityException],
    policy_version: str,
    now: datetime,
) -> GateResult:
    components = set(components)
    reasons: list[str] = []
    ok_scans = [s for s in scans if s.status == "success" and now - s.observed_at <= SCAN_FRESHNESS and s.observed_at <= now]
    for s in scans:
        if s.status != "success":
            reasons.append(f"scan {s.scan_run_id} did not complete ({s.status})")
        elif now - s.observed_at > SCAN_FRESHNESS:
            reasons.append(f"scan {s.scan_run_id} is older than 24 hours")
    covered = set().union(*[s.coverage for s in ok_scans]) if ok_scans else set()
    missing = sorted(components - covered)
    bound_scans = tuple(s.scan_run_id for s in ok_scans)
    in_scope_ids = {fid for s in ok_scans for fid in s.finding_ids}
    in_scope = [f for f in findings if f.finding_id in in_scope_ids and f.component in components]
    finding_view = tuple(
        {"finding_id": f.finding_id, "component": f.component, "severity": f.severity,
         "fix_version": f.fix_version, "record_id": f.record_id, "blocking": blocks(f),
         "module": f.module, "version": f.version, "advisory": f.advisory, "summary": f.summary,
         "required_scopes": ["restricted:security"]}
        for f in in_scope
    )
    exc_by_finding = {e.finding_id: e for e in exceptions}
    used_exceptions: list[SecurityException] = []

    def result(state: SecurityState, signoff: bool = False) -> GateResult:
        expiries = [s.observed_at + SCAN_FRESHNESS for s in ok_scans] + [e.expires for e in used_exceptions if e.expires]
        return GateResult(
            decision_id=f"sec-{uuid.uuid4().hex[:10]}", state=state, findings=finding_view,
            scan_run_ids=bound_scans, exception_ids=tuple(e.exception_id for e in used_exceptions),
            policy_version=policy_version, valid_until=min(expiries) if (expiries and state != SecurityState.NOT_RUN) else None,
            signoff_required=signoff, reasons=tuple(reasons),
        )

    if not components:
        reasons.append("no components in scope")
        return result(SecurityState.NOT_RUN)
    if missing:
        reasons.append("components without a successful fresh scan: " + ", ".join(missing))
        return result(SecurityState.NOT_RUN)
    blocking = [f for f in in_scope if blocks(f)]
    if not blocking:
        return result(SecurityState.PASSED)
    unexcepted = [f for f in blocking if not exception_valid(exc_by_finding.get(f.finding_id), f.finding_id, now)]
    if unexcepted:
        reasons.append("blocking findings: " + ", ".join(f.finding_id for f in unexcepted))
        return result(SecurityState.BLOCKED)
    used_exceptions.extend(exc_by_finding[f.finding_id] for f in blocking)
    return result(SecurityState.ACCEPTED_WITH_EXCEPTION, signoff=True)


def revalidate(
    result: GateResult | Mapping[str, Any],
    now: datetime,
    current_policy_version: str,
    exception_lookup: Callable[[str], SecurityException | None] = lambda _id: None,
    signed_off: bool = False,
) -> tuple[bool, str]:
    """Fail closed unless the decision is still valid at ``now``."""
    r = result if isinstance(result, GateResult) else _from_mapping(result)
    if r.state not in (SecurityState.PASSED, SecurityState.ACCEPTED_WITH_EXCEPTION):
        return False, f"security state is {r.state}"
    if r.valid_until is None or now > r.valid_until:
        return False, "security decision expired"
    if r.policy_version != current_policy_version:
        return False, "security policy changed"
    for eid in r.exception_ids:
        e = exception_lookup(eid)
        if e is None or e.revoked or e.expires is None or e.expires <= now:
            return False, f"exception {eid} is no longer valid"
    if r.state == SecurityState.ACCEPTED_WITH_EXCEPTION and not signed_off:
        return False, "security sign-off required"
    return True, "valid"


def _from_mapping(m: Mapping[str, Any]) -> GateResult:
    vu = m.get("valid_until")
    return GateResult(
        decision_id=m.get("decision_id", ""), state=SecurityState(m["state"]), findings=tuple(m.get("findings", ())),
        scan_run_ids=tuple(m.get("scan_run_ids", ())), exception_ids=tuple(m.get("exception_ids", ()) or ([m["exception_ref"]] if m.get("exception_ref") else [])),
        policy_version=m.get("policy_version", ""), valid_until=datetime.fromisoformat(vu.replace("Z", "+00:00")) if isinstance(vu, str) else vu,
        signoff_required=bool(m.get("signoff_required")),
    )


# ---------------------------------------------------------------------------
# Read paths
# ---------------------------------------------------------------------------


def parse_security_records(records: Iterable[Record], content_of: Callable[[Record], str] = lambda r: r.content_ref) -> tuple[list[ScanRun], list[Finding]]:
    """Turn security records into scan runs and findings. Unreadable records are ignored (not clean)."""
    scans: list[ScanRun] = []
    findings: list[Finding] = []
    for rec in records:
        try:
            data = json.loads(content_of(rec))
        except (ValueError, TypeError):
            continue
        if data.get("kind") == "scan_run":
            scans.append(ScanRun(
                scan_run_id=data["scan_run_id"], observed_at=rec.confirmed_through or rec.observed_at,
                coverage=frozenset(data.get("coverage", [])), status=data.get("status", "error"),
                finding_ids=tuple(data.get("finding_ids", [])), target_revision=data.get("target_revision"),
            ))
        elif data.get("kind") == "finding":
            findings.append(Finding(data["finding_id"], data.get("component", ""), data.get("severity"),
                                    data.get("fix_version"), rec.record_id, module=data.get("module"),
                                    version=data.get("version"), advisory=(data.get("advisory_ids") or [None])[0],
                                    summary=data.get("summary")))
    return scans, findings


class SecurityGate:
    def __init__(self, policy_version: str, clock: Callable[[], datetime]) -> None:
        self.policy_version = policy_version
        self.clock = clock

    def evaluate_for_run(self, resolver: Any, components: Iterable[str],
                         exceptions: Sequence[SecurityException] = ()) -> GateResult:
        """Investigation gating: reads only through the run's snapshot resolver."""
        records = [r.record for r in resolver.search({"source": "security"})]
        scans, findings = parse_security_records(records, resolver.content_of)
        return evaluate(components, scans, findings, exceptions, self.policy_version, resolver.ctx.evidence_clock)

    def evaluate_for_task(self, reader: SecurityReader, ctx: SecurityContext, components: Iterable[str],
                          exceptions: Sequence[SecurityException] = ()) -> GateResult:
        """Task gating: reads only the task's own action-security partition."""
        if not isinstance(ctx, SecurityContext):
            raise TypeError("task gating requires a SecurityContext")
        scans, findings = parse_security_records(reader.read(ctx))
        scans = [s for s in scans if s.target_revision in (None, ctx.scanned_revision)]
        return evaluate(components, scans, findings, exceptions, self.policy_version, self.clock())

    def revalidate(self, result: GateResult | Mapping[str, Any], now: datetime | None = None,
                   exception_lookup: Callable[[str], SecurityException | None] = lambda _id: None,
                   signed_off: bool = False) -> tuple[bool, str]:
        return revalidate(result, now or self.clock(), self.policy_version, exception_lookup, signed_off)
