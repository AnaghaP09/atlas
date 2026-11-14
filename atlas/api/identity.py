"""Who is calling. The principal is resolved in exactly one place.

Header mode (this build): the ``X-Atlas-Role`` header names one demo role. It stands in for sign-in,
so the server only runs on a loopback address in this mode. A session-cookie mode for built-in
sign-in plugs in here later without touching any route: routes only ever call ``principal_of``.
No query parameter or body field can name another principal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from fastapi import Request

from atlas.contracts import Principal, Role
from atlas.projection import principal_for
from atlas.projects import ANY_PROJECT, ProjectRegistry

ROLE_HEADER = "x-atlas-role"


class IdentityError(Exception):
    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


class IdentityResolver(Protocol):
    mode: str

    def resolve(self, request: Request) -> Principal | None:
        """The caller's principal, or None when the request carries no identity."""


def requested_project(request: Request) -> str | None:
    """The project a request acts in: the ``project_id`` path parameter, else the query parameter."""
    path_params = getattr(request, "path_params", None) or {}
    value = path_params.get("project_id") or request.query_params.get("project_id")
    return str(value) if value else None


@dataclass
class HeaderIdentity:
    """Demo identity: one role, valid in every configured project (header mode only)."""

    mode: str = "header"
    projects: ProjectRegistry = field(default_factory=ProjectRegistry)
    default_project: str | None = None

    def project_for(self, request: Request) -> str | None:
        requested = requested_project(request)
        if requested is not None:
            if not self.projects.exists(requested):
                raise IdentityError(404, "unknown project")
            return requested
        if self.default_project and self.projects.exists(self.default_project):
            return self.default_project
        ids = self.projects.ids()
        return ids[0] if ids else ANY_PROJECT

    def resolve(self, request: Request) -> Principal | None:
        raw = request.headers.get(ROLE_HEADER)
        if raw is None or raw.strip() == "":
            return None
        value = raw.strip().lower()
        if value not in {r.value for r in Role}:
            raise IdentityError(401, "unknown role in the X-Atlas-Role header")
        return principal_for(value, project_id=self.project_for(request))

    def may_use_project(self, principal: Principal, project_id: str) -> bool:
        if principal.project_id == ANY_PROJECT:
            return True
        return self.projects.exists(project_id) and principal.project_id == project_id


def principal_of(request: Request) -> Principal:
    """The single entry point every route uses. Raises 401 when no identity is present."""
    resolver: IdentityResolver = request.app.state.identity
    principal = resolver.resolve(request)
    if principal is None:
        raise IdentityError(401, "sign-in required: send the X-Atlas-Role header in this demo build")
    return principal


def require_role(principal: Principal, allowed: set[str], action: str) -> None:
    if principal.role is None or principal.role.value not in allowed:
        raise IdentityError(403, f"the {principal.role.value if principal.role else 'public'} role may not {action}")
