"""Authorization projection: the only path by which content leaves the backend.

Every scope-bearing object is kept only if the principal holds all of its ``required_scopes``, and
every object that names a ``project_id`` is kept only for a principal acting in that project.
Objects whose scopes are unknown are withheld from everyone on every surface except the audit
endpoint. A caller-supplied reader can only narrow the principal, never widen it. Content projected
for the ``public`` principal is also scanned for secrets and personal data; any hit fails the step
closed and is reported to containment.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from atlas.contracts import PUBLIC_PRINCIPAL, Principal, Role
from atlas.outbound import Redactor
from atlas.projects import ANY_PROJECT

# Scopes each demo role holds. ``restricted:security`` covers scanner details.
ROLE_SCOPES: dict[str, frozenset[str]] = {
    "pm": frozenset({"public", "internal"}),
    "em": frozenset({"public", "internal", "staffing"}),
    "architect": frozenset({"public", "internal"}),
    "security": frozenset({"public", "internal", "restricted:security"}),
    "oncall": frozenset({"public", "internal"}),
    "audit": frozenset({"public", "internal", "staffing", "restricted:security"}),
}

PUBLIC = Principal(role=None, scopes=frozenset({"public"}))
UNKNOWN = object()


def principal_for(role: str | None, project_id: str | None = None, user: str | None = None) -> Principal:
    """The principal for ``role`` acting in ``project_id``.

    The public principal is bound to a project too when it receives one project's content
    (publication); its export grant is checked by the caller before projecting.
    """
    if role is None or role == PUBLIC_PRINCIPAL:
        return PUBLIC if project_id is None else Principal(role=None, scopes=PUBLIC.scopes, project_id=project_id)
    return Principal(role=Role(role), scopes=ROLE_SCOPES[role], user=user, project_id=project_id)


def narrow(identity: Principal, requested_reader: str | None) -> Principal:
    """A caller may view as a narrower reader, never a wider one, and never in another project."""
    if requested_reader is None:
        return identity
    other = principal_for(requested_reader)
    return Principal(role=other.role, scopes=identity.scopes & other.scopes, user=identity.user,
                     project_id=identity.project_id)


def granted_principal(principal: Principal, object_scopes: Any) -> Principal:
    """The reader for one granted object: the same person, plus exactly that object's own scopes."""
    return Principal(role=principal.role, scopes=frozenset(principal.scopes) | frozenset(object_scopes),
                     user=principal.user, project_id=principal.project_id)


def _outside_project(value: Mapping[str, Any], principal: Principal) -> bool:
    """True when ``value`` belongs to a project other than the principal's.

    Objects with no ``project_id`` (installation-wide items such as the kill switches) stay visible.
    A principal with no project sees only installation-wide items and objects of no project.
    """
    owner = value.get("project_id")
    if owner in (None, "") or principal.project_id == ANY_PROJECT:
        return False
    return owner != principal.project_id


class DisclosureBlocked(RuntimeError):
    """Public content still contained personal data or a secret."""


class EventSink(Protocol):
    def record_event(self, event_type: str, detail: Mapping[str, Any]) -> None: ...


@dataclass
class ProjectionResult:
    artifact: dict[str, Any]
    withheld: int


class AuthorizationProjection:
    def __init__(self, events: EventSink | None = None, redactor: Redactor | None = None) -> None:
        self.events = events
        self.redactor = redactor or Redactor()

    def _walk(self, value: Any, principal: Principal, audit_endpoint: bool, counter: list[int]) -> Any:
        if isinstance(value, Mapping):
            if _outside_project(value, principal):
                counter[0] += 1
                return UNKNOWN
            if "required_scopes" in value or "access_scopes" in value:
                scopes = value.get("required_scopes", value.get("access_scopes"))
                if scopes is None:
                    if not (audit_endpoint and principal.role == Role.AUDIT):
                        counter[0] += 1
                        return UNKNOWN
                elif not set(scopes) <= set(principal.scopes):
                    counter[0] += 1
                    return UNKNOWN
            out = {}
            for k, v in value.items():
                pv = self._walk(v, principal, audit_endpoint, counter)
                out[k] = None if pv is UNKNOWN else pv
            return out
        if isinstance(value, (list, tuple)):
            kept = []
            for item in value:
                pv = self._walk(item, principal, audit_endpoint, counter)
                if pv is not UNKNOWN:
                    kept.append(pv)
            return kept
        return value

    def project_with_count(self, artifact: Mapping[str, Any], principal: Principal, *, audit_endpoint: bool = False) -> ProjectionResult:
        counter = [0]
        top = dict(artifact)
        top_scopes = top.get("required_scopes", [])
        if _outside_project(top, principal):
            return ProjectionResult({"restricted_items": 1, "withheld": True}, 1)
        if top_scopes is not None and not set(top_scopes) <= set(principal.scopes):
            return ProjectionResult({"restricted_items": 1, "withheld": True}, 1)
        projected = self._walk({k: v for k, v in top.items()}, principal, audit_endpoint, counter)
        if projected is UNKNOWN:
            projected = {}
        projected["restricted_items"] = counter[0] + int(top.get("restricted_items", 0) or 0)
        if principal.is_public:
            self._public_scan(projected)
        return ProjectionResult(projected, counter[0])

    def project(self, artifact: Mapping[str, Any], principal: Principal, *,
                grant: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Project for ``principal``. With an active read-only ``grant`` for this one object, the
        object's own scopes are honoured for this read; nothing else is widened, so content inside
        it that needs a further scope (for example restricted security detail) stays withheld."""
        if grant is not None:
            principal = granted_principal(principal, grant.get("object_scopes") or ())
        return self.project_with_count(artifact, principal).artifact

    def project_for_audit_endpoint(self, artifact: Mapping[str, Any], principal: Principal) -> dict[str, Any]:
        if principal.role != Role.AUDIT:
            raise PermissionError("the audit endpoint is for the audit role only")
        return self.project_with_count(artifact, principal, audit_endpoint=True).artifact

    def _public_scan(self, value: Any) -> None:
        texts: list[str] = []

        def collect(v: Any) -> None:
            if isinstance(v, str):
                texts.append(v)
            elif isinstance(v, Mapping):
                for x in v.values():
                    collect(x)
            elif isinstance(v, (list, tuple)):
                for x in v:
                    collect(x)

        collect(value)
        for t in texts:
            found = self.redactor.scan(t)
            r = self.redactor.redact(t)
            sensitive = set(found) | {k for k in r.counts if k in {"token", "private_key", "secret_field", "email", "handle", "person", "end_user", "ip_address", "hostname"}}
            if sensitive:
                if self.events:
                    self.events.record_event("unauthorized_disclosure_blocked", {"categories": sorted(sensitive)})
                raise DisclosureBlocked("public content contains " + ", ".join(sorted(sensitive)))
