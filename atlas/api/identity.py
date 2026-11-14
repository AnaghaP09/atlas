"""Who is calling. The principal is resolved in exactly one place.

Header mode (this build): the ``X-Atlas-Role`` header names one demo role. It stands in for sign-in,
so the server only runs on a loopback address in this mode. A session-cookie mode for built-in
sign-in plugs in here later without touching any route: routes only ever call ``principal_of``.
No query parameter or body field can name another principal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from fastapi import Request

from atlas.contracts import Principal, Role
from atlas.projection import principal_for

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


@dataclass
class HeaderIdentity:
    mode: str = "header"

    def resolve(self, request: Request) -> Principal | None:
        raw = request.headers.get(ROLE_HEADER)
        if raw is None or raw.strip() == "":
            return None
        value = raw.strip().lower()
        if value not in {r.value for r in Role}:
            raise IdentityError(401, "unknown role in the X-Atlas-Role header")
        return principal_for(value)


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
