"""The FastAPI application: one ``/api`` prefix, Host and Origin checks before routing, JSON errors,
and the built UI at ``/`` through a fallback that never answers an ``/api`` path.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from atlas.api import (metrics, navigation, routes_access, routes_eval, routes_ops, routes_projects, routes_runs,
                       routes_tasks)
from atlas.api.guard import HostOriginGuard
from atlas.api.identity import IdentityError
from atlas.api.idempotency import IdempotencyError
from atlas.api.ports import NotWired
from atlas.api.services import Services
from atlas.contracts import RunContext, RunMode, StartedBy, StartedByKind, Role
from atlas.scheduler import Dispatcher

UI_DIST = Path(__file__).resolve().parents[2] / "ui" / "dist"
CSP = ("default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
       "form-action 'none'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'")
# The sign-in page posts its form to this server, so only that page allows a same-origin form action.
LOGIN_CSP = CSP.replace("form-action 'none'", "form-action 'self'")
LOGIN_PATHS = ("/login", "/logout")


def _error(status: int, error: str, reason: str, **extra: Any) -> JSONResponse:
    return JSONResponse({"error": error, "reason": reason, **extra}, status_code=status)


def _start_run_factory(services: Services):
    def start_run(adm: Any) -> None:
        if services.orchestrator is None:
            services.run_errors[adm.run_id] = "the run engine is not connected in this build yet"
            return
        try:
            snap = dict(services.snapshots.get(adm.snapshot_id) or {})
        except NotWired as exc:
            services.run_errors[adm.run_id] = str(exc)
            return
        cutoff = snap.get("cutoff")
        cutoff_dt = datetime.fromisoformat(cutoff) if isinstance(cutoff, str) else (cutoff or datetime.now(timezone.utc))
        ctx = RunContext(run_id=adm.run_id, snapshot_id=adm.snapshot_id, mode=RunMode(snap.get("mode", "replay")),
                         release_or_case=str(snap.get("release") or snap.get("case_id") or ""), cutoff=cutoff_dt,
                         alias_variant=snap.get("alias_variant"), evidence_clock=cutoff_dt,
                         project_id=adm.project_id or str(snap.get("project_id") or ""))
        sb = adm.started_by
        started_by = StartedBy(kind=StartedByKind(sb.get("kind", "person")),
                               role=Role(sb["role"]) if sb.get("role") else None, watcher_id=sb.get("watcher_id"))
        try:
            services.orchestrator.run(adm.run_id, ctx, adm.trigger, started_by)
        except Exception as exc:  # a failed run must end in a notification, never silently
            services.run_errors[adm.run_id] = f"{type(exc).__name__}: {exc}"
            try:
                services.notifier.emit("run.failed", adm.run_id, [adm.run_id], frozenset({"internal"}),
                                       {"text": f"run {adm.run_id} failed before completing", "required_scopes": ["internal"]})
            except Exception:
                pass

    return start_run


def _work_allowed(services: Services):
    def check() -> str | None:
        if services.spend is not None and services.spend.hold_reason():
            return services.spend.hold_reason()
        permits = services.containment.permits("model_calls")
        if not getattr(permits, "allowed", True):
            return "containment: " + "; ".join(getattr(permits, "reasons", ()) or ("model calls paused",))
        if services.mode.get().value == "offline":
            return "offline"
        return None

    return check


def create_app(services: Services, *, site: Any = None, sessions: Any = None,
               extra_routers: tuple[Any, ...] = ()) -> FastAPI:
    """Build the app. With ``site`` (sign-in mode) the site guard replaces the loopback host guard,
    and ``extra_routers`` (the sign-in routes) are mounted before the UI fallback."""
    app = FastAPI(title="ATLAS API", version="1", openapi_url="/api/openapi.json", docs_url="/api/docs",
                  redoc_url=None)
    app.state.services = services
    metrics.install(services)
    app.state.identity = services.identity
    services.dispatcher = Dispatcher(services.admissions, _start_run_factory(services), _work_allowed(services))

    api = APIRouter(prefix="/api")
    api.include_router(routes_runs.router)
    api.include_router(routes_tasks.router)
    api.include_router(routes_ops.router)
    api.include_router(routes_eval.router)
    api.include_router(routes_projects.router)
    api.include_router(navigation.router)
    api.include_router(metrics.router)
    api.include_router(routes_access.router)
    if services.is_scenario:
        api.include_router(routes_eval.scenario_router)
    app.include_router(api)
    if site is None:
        # Without sign-in there is no session. Say so plainly, so the UI does not have to guess
        # from a missing route (the sign-in site serves the real ``/api/session``).
        @app.get("/api/session", include_in_schema=False)
        async def _no_session() -> dict[str, Any]:
            return {"sign_in": False, "identity": "header"}

    from atlas.api import guide

    app.include_router(guide.router)
    for extra in extra_routers:
        app.include_router(extra)
    app.state.sessions = sessions

    @app.exception_handler(IdentityError)
    async def _identity(_: Request, exc: IdentityError) -> JSONResponse:
        return _error(exc.status, "unauthorised" if exc.status == 401 else "forbidden", exc.reason)

    @app.exception_handler(IdempotencyError)
    async def _idem(_: Request, exc: IdempotencyError) -> JSONResponse:
        return _error(exc.status, exc.error, exc.reason)

    from atlas.api.support import ApiError

    @app.exception_handler(ApiError)
    async def _api(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(exc.body(), status_code=exc.status)

    @app.exception_handler(NotWired)
    async def _wired(_: Request, exc: NotWired) -> JSONResponse:
        return _error(503, "not_connected", str(exc))

    @app.exception_handler(RequestValidationError)
    async def _invalid(_: Request, exc: RequestValidationError) -> JSONResponse:
        return _error(400, "invalid_request", "the request body or parameters are not valid")

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 404:
            return _error(404, "not_found", "no such route" if request.url.path.startswith("/api") else "not found")
        if exc.status_code == 405:
            return _error(405, "method_not_allowed", "this method is not allowed on this route")
        return _error(exc.status_code, "error", str(exc.detail))

    @app.middleware("http")
    async def _security_headers(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        if not request.url.path.startswith("/api"):
            path = request.url.path
            response.headers["Content-Security-Policy"] = LOGIN_CSP if path in LOGIN_PATHS else CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        # same-origin: our own form posts keep their real Origin (a sign-in form posted under
        # no-referrer arrives as Origin: null and is refused); other sites still get no referrer.
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    @app.get("/{path:path}", include_in_schema=False)
    async def _ui(path: str) -> Any:
        if path == "api" or path.startswith("api/"):
            return _error(404, "not_found", "no such route")
        index = UI_DIST / "index.html"
        candidate = (UI_DIST / path).resolve()
        if path and UI_DIST.exists() and candidate.is_file() and UI_DIST.resolve() in candidate.parents:
            return FileResponse(candidate)
        if index.exists():
            return FileResponse(index)
        return _error(404, "not_found", "the UI has not been built on this server")

    if site is not None:
        from atlas.site import SiteGuard

        app.add_middleware(SiteGuard, site=site, sessions=sessions)
        return app
    if services.ui_origins:
        app.add_middleware(CORSMiddleware, allow_origins=sorted(services.ui_origins), allow_methods=["GET", "POST"],
                           allow_headers=["X-Atlas-Role", "Idempotency-Key", "Last-Event-ID", "Content-Type"])
    app.add_middleware(HostOriginGuard, allowed_hosts=services.allowed_hosts, allowed_origins=services.allowed_origins)
    return app
