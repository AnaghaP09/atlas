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
    """Resolve first, then authorise: the object must exist and belong to a project the caller may use.

    In header mode every demo role may use every project; the session mode adds membership here.
    """
    if obj is None:
        raise ApiError(404, "not_found", f"{what} not found")
    resolver = services_of(request).identity
    allowed = getattr(resolver, "may_use_project", None)
    project_id = obj.get("project_id")
    if allowed is not None and project_id is not None and not allowed(principal, project_id):
        raise ApiError(404, "not_found", f"{what} not found")
    return obj


def idempotent(request: Request, principal: Principal, body: Any,
               effect: Callable[[str], tuple[int, dict[str, Any]]]) -> JSONResponse:
    """Run a manual mutation once per Idempotency-Key; repeats return the stored outcome."""
    key = validate_key(request.headers.get("idempotency-key"))
    services = services_of(request)
    who = principal.role.value if principal.role else "public"
    h = request_hash(request.method, request.url.path, body)
    outcome = services.idempotency.run(key, who, h, lambda: effect(key))
    headers = {"Idempotent-Replay": "true"} if outcome.replay else {}
    return JSONResponse(outcome.body, status_code=outcome.status, headers=headers)


__all__ = ["ApiError", "IdempotencyError", "check_project", "envelope", "idempotent", "services_of", "staged"]
