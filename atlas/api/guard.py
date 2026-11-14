"""Host and Origin checks, applied before routing.

Every request must carry a Host header from an exact allow-list. A request that carries an Origin
header must come from an allowed origin. This blocks pages on other sites, including pages that use
DNS rebinding to reach a server on 127.0.0.1. Non-browser clients send no Origin and pass with an
allowed Host.
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Iterable

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


class HostOriginGuard:
    def __init__(self, app: Any, allowed_hosts: Iterable[str], allowed_origins: Iterable[str]) -> None:
        self.app = app
        self.allowed_hosts = {h.lower() for h in allowed_hosts}
        self.allowed_origins = {o.lower().rstrip("/") for o in allowed_origins}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        host = headers.get("host", "").lower()
        if host not in self.allowed_hosts:
            await self._reject(send, 421, "host_not_allowed", "this server only answers its own loopback address")
            return
        origin = headers.get("origin")
        if origin is not None and origin.lower().rstrip("/") not in self.allowed_origins:
            await self._reject(send, 403, "origin_not_allowed", "requests from this origin are not accepted")
            return
        await self.app(scope, receive, send)

    @staticmethod
    async def _reject(send: Send, status: int, error: str, reason: str) -> None:
        body = json.dumps({"error": error, "reason": reason}).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
