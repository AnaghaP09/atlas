"""The customer-facing HTTPS site.

ATLAS serves HTTPS itself on the loopback address. Each server has one host name built from the
organisation slug (``ATLAS_ORG_SLUG``):

- the main server: ``https://atlas.<org>.internal`` on 127.0.0.1:443
- the scenario server: ``https://scenario.<org>.internal:8443`` on 127.0.0.1:8443

The site guard runs before routing:
- The Host header must be the server's own host name. ``127.0.0.1:<port>`` is accepted only for
  ``GET /api/health`` (the local start-up probe).
- Safe requests (GET, HEAD, OPTIONS) may have no Origin header, for example the first page load.
  If an Origin is present it must be the server's own origin.
- Every other request must carry the server's exact Origin. Requests under ``/api`` must also carry
  the session's CSRF token in the ``X-CSRF-Token`` header. The sign-in form has its own CSRF check.
- A page request without a session is sent to the sign-in page.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from atlas.identity import CSRF_HEADER, ROLE_HEADER
from atlas.sessions import COOKIE_NAME, SessionStore

MAIN_PORT = 443
SCENARIO_PORT = 8443
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
PUBLIC_PATHS = {"/login", "/login.css", "/api/health"}


@dataclass(frozen=True)
class SiteConfig:
    org_slug: str
    scenario: bool = False
    domain: str = "internal"
    bind_host: str = "127.0.0.1"
    port: int | None = None
    data_dir: Path = Path("data")

    @property
    def listen_port(self) -> int:
        return self.port if self.port is not None else (SCENARIO_PORT if self.scenario else MAIN_PORT)

    @property
    def host_name(self) -> str:
        return f"{'scenario' if self.scenario else 'atlas'}.{self.org_slug}.{self.domain}"

    @property
    def origin(self) -> str:
        if self.listen_port == 443:
            return f"https://{self.host_name}"
        return f"https://{self.host_name}:{self.listen_port}"

    @property
    def allowed_hosts(self) -> frozenset[str]:
        hosts = {f"{self.host_name}:{self.listen_port}"}
        if self.listen_port == 443:
            hosts.add(self.host_name)
        return frozenset(hosts)

    @property
    def probe_hosts(self) -> frozenset[str]:
        return frozenset({f"127.0.0.1:{self.listen_port}"})

    @property
    def tls_dir(self) -> Path:
        return self.data_dir / "local" / "tls"

    @property
    def cert_file(self) -> Path:
        return self.tls_dir / "server.crt"

    @property
    def key_file(self) -> Path:
        return self.tls_dir / "server.key"

    @property
    def session_db(self) -> Path:
        name = "sessions-scenario.sqlite" if self.scenario else "sessions.sqlite"
        return self.data_dir / "local" / name

    def uvicorn_options(self) -> dict[str, Any]:
        """Keyword arguments for ``uvicorn.run`` to serve this site over HTTPS."""
        if not self.cert_file.is_file() or not self.key_file.is_file():
            raise FileNotFoundError("server certificate not found; run `python -m deploy.init` first")
        return {
            "host": self.bind_host,
            "port": self.listen_port,
            "ssl_certfile": str(self.cert_file),
            "ssl_keyfile": str(self.key_file),
            "proxy_headers": False,
            "server_header": False,
        }


Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


def _cookie(headers: dict[str, str], name: str) -> str | None:
    for part in headers.get("cookie", "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return None


class SiteGuard:
    """ASGI middleware for the HTTPS site (sign-in mode). Replaces the loopback host guard."""

    def __init__(self, app: Any, site: SiteConfig, sessions: SessionStore) -> None:
        self.app = app
        self.site = site
        self.sessions = sessions

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        method = scope.get("method", "GET").upper()
        path = scope.get("path", "/")
        host = headers.get("host", "").lower()

        if host in self.site.probe_hosts:
            if not (method == "GET" and path == "/api/health"):
                await _reject(send, 421, "host_not_allowed", "use the site address, not the loopback address")
                return
        elif host not in self.site.allowed_hosts:
            await _reject(send, 421, "host_not_allowed", "this server only answers its own address")
            return

        if ROLE_HEADER in headers:
            await _reject(send, 401, "unauthorised", "the demo role header is not accepted when sign-in is on")
            return

        origin = headers.get("origin")
        if origin is not None and origin.rstrip("/").lower() != self.site.origin.lower():
            await _reject(send, 403, "origin_not_allowed", "requests from this origin are not accepted")
            return

        if method not in SAFE_METHODS:
            if origin is None:
                await _reject(send, 403, "origin_required", "state-changing requests must come from the ATLAS page")
                return
            if path.startswith("/api/"):
                session = self.sessions.validate(_cookie(headers, COOKIE_NAME), touch=False)
                if session is None:
                    await _reject(send, 401, "unauthorised", "sign-in required")
                    return
                if not session.csrf_ok(headers.get(CSRF_HEADER)):
                    await _reject(send, 403, "csrf_failed", "missing or wrong CSRF token")
                    return
        elif not path.startswith("/api/") and path not in PUBLIC_PATHS:
            if self.sessions.validate(_cookie(headers, COOKIE_NAME), touch=False) is None:
                await send({"type": "http.response.start", "status": 303,
                            "headers": [(b"location", b"/login"), (b"content-length", b"0"),
                                        (b"cache-control", b"no-store")]})
                await send({"type": "http.response.body", "body": b""})
                return

        await self.app(scope, receive, send)


async def _reject(send: Send, status: int, error: str, reason: str) -> None:
    body = json.dumps({"error": error, "reason": reason}).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                            (b"cache-control", b"no-store")]})
    await send({"type": "http.response.body", "body": body})
