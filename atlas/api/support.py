"""Shared helpers for every route: the response envelope, errors, idempotent mutations and the
project check. Every response body leaves through ``envelope`` (or ``envelope_list``), which projects
it for the caller; a test walks every route in the OpenAPI document to confirm it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from fastapi import Request
from fastapi.responses import JSONResponse

from atlas.api.idempotency import IdempotencyError, request_hash, validate_key
from atlas.contracts import Principal
from atlas.projects import ANY_PROJECT

DATA_MODES = {"REAL", "SYNTHETIC", "STAGED", "UNEVALUATED"}


class ApiError(Exception):
    def __init__(self, status: int, error: str, reason: str, notification_id: str | None = None) -> None:
        super().__init__(reason)
        self.status = status
        self.error = error
        self.reason = reason
        self.notification_id = notification_id

    def body(self) -> dict[str, Any]:
        out: dict[str, Any] = {"error": self.error, "reason": self.reason}
        if self.notification_id:
            out["notification_id"] = self.notification_id
        return out


def services_of(request: Request) -> Any:
    return request.app.state.services


def envelope(request: Request, principal: Principal, payload: Mapping[str, Any], *, data_mode: str = "REAL",
             audit_endpoint: bool = False) -> dict[str, Any]:
    """Project ``payload`` for the caller and add the envelope fields."""
    if data_mode not in DATA_MODES:
        raise ValueError(f"unknown data mode {data_mode}")
    services = services_of(request)
    artefact = dict(payload)
    artefact.setdefault("required_scopes", [])
    if audit_endpoint:
        projected = services.projection.project_for_audit_endpoint(artefact, principal)
    else:
        projected = services.projection.project(artefact, principal)
    projected.pop("required_scopes", None)
    projected.setdefault("data_mode", data_mode)
    projected["environment"] = services.environment
    projected["server_time"] = datetime.now(timezone.utc).isoformat()
    return projected


def staged(request: Request, principal: Principal, reason: str, **extra: Any) -> dict[str, Any]:
    return envelope(request, principal, {"status": "not_available_in_demo", "reason": reason, **extra},
                    data_mode="STAGED")


def check_project(request: Request, principal: Principal, obj: Mapping[str, Any] | None, what: str) -> Mapping[str, Any]:
    """Resolve first, then authorise, before any effect.

    The object must exist and its STORED ``project_id`` must be the project the caller acts in (the
    caller's role is their role in that project). A ``project_id`` sent in the request body is never
    used for this check. An object of another project answers exactly like a missing one, so its
    existence is not revealed. Objects with no project (installation-wide items) are allowed.
    """
    if obj is None:
        raise ApiError(404, "not_found", f"{what} not found")
    owner = obj.get("project_id")
    if owner not in (None, "") and principal.project_id != ANY_PROJECT and owner != principal.project_id:
        raise ApiError(404, "not_found", f"{what} not found")
    return obj


def scoped_key(principal: Principal, key: str) -> str:
    """Idempotency keys are partitioned by project: the same key in two projects is two requests."""
    return f"{principal.project_id or '-'}::{key}"


def idempotent(request: Request, principal: Principal, body: Any,
               effect: Callable[[str], tuple[int, dict[str, Any]]]) -> JSONResponse:
    """Run a manual mutation once per Idempotency-Key; repeats return the stored outcome."""
    key = scoped_key(principal, validate_key(request.headers.get("idempotency-key")))
    services = services_of(request)
    who = f"{principal.project_id or '-'}:{principal.role.value if principal.role else 'public'}"
    h = request_hash(request.method, request.url.path, body)
    outcome = services.idempotency.run(key, who, h, lambda: effect(key))
    headers = {"Idempotent-Replay": "true"} if outcome.replay else {}
    return JSONResponse(outcome.body, status_code=outcome.status, headers=headers)


__all__ = ["ApiError", "IdempotencyError", "check_project", "envelope", "idempotent", "scoped_key", "services_of",
           "staged"]
