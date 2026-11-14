"""Sign-in mode for the demo runner.

When ATLAS runs as the HTTPS site with built-in sign-in, the demo runner cannot send a role header.
Instead it signs in as the demo user for each role the manifest names, the way a browser does:

- one cookie jar per server and user (the normal and scenario servers never share a session);
- the sign-in form's CSRF field and cookie;
- the exact Origin of the server on every state-changing request;
- the session's CSRF token in ``X-CSRF-Token`` on every write.

Demo user names are the role names (``pm``, ``em``, ...). Their passwords come from environment
variables such as ``ATLAS_DEMO_PASSWORD_EM``; they are never read from or written to a file.
The local certificate authority made by setup is trusted for these requests only.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

ROLE_HEADER = "X-Atlas-Role"
CSRF_FIELD = re.compile(r'name="csrf" value="([^"]+)"')
DEFAULT_ROLE = "pm"


class SignInError(RuntimeError):
    pass


def origin_of(base: str) -> str:
    parts = urlsplit(base)
    return f"{parts.scheme}://{parts.netloc}"


def password_from_env(role: str, environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    value = env.get(f"ATLAS_DEMO_PASSWORD_{role.upper()}", "")
    if not value:
        raise SignInError(f"set ATLAS_DEMO_PASSWORD_{role.upper()} to the {role} demo user's password")
    return value


class SessionSend:
    """A ``send`` function for the demo runner that signs in per server and role."""

    def __init__(self, make_client: Callable[[], Any], password_for: Callable[[str], str] = password_from_env) -> None:
        self.make_client = make_client
        self.password_for = password_for
        self._sessions: dict[tuple[str, str], tuple[Any, str]] = {}

    def _signed_in(self, base: str, role: str) -> tuple[Any, str]:
        key = (base, role)
        if key in self._sessions:
            return self._sessions[key]
        client = self.make_client()
        page = client.get(base + "/login")
        match = CSRF_FIELD.search(page.text or "")
        if page.status_code != 200 or match is None:
            raise SignInError(f"the sign-in page at {base} did not load ({page.status_code})")
        r = client.post(base + "/login", data={"user": role, "password": self.password_for(role), "csrf": match.group(1)},
                        headers={"Origin": origin_of(base)})
        if r.status_code not in (302, 303):
            raise SignInError(f"sign-in as {role} at {base} failed ({r.status_code})")
        session = client.get(base + "/api/session")
        if session.status_code != 200:
            raise SignInError(f"no session after signing in as {role} at {base} ({session.status_code})")
        csrf = session.json().get("csrf_token", "")
        self._sessions[key] = (client, csrf)
        return client, csrf

    def __call__(self, method: str, base: str, path: str, headers: Mapping[str, str], body: Any, params: Any):
        sent = {k: v for k, v in headers.items() if k.lower() != ROLE_HEADER.lower()}
        role = next((v for k, v in headers.items() if k.lower() == ROLE_HEADER.lower()), DEFAULT_ROLE)
        client, csrf = self._signed_in(base, role)
        if method.upper() not in ("GET", "HEAD", "OPTIONS"):
            sent["Origin"] = origin_of(base)
            sent["X-CSRF-Token"] = csrf
        r = client.request(method, base + path, headers=sent, json=body, params=params)
        try:
            data = r.json()
        except ValueError:
            data = r.text
        return r.status_code, r.headers, data


def httpx_session_send(data_dir: Path) -> SessionSend:
    import httpx

    ca = data_dir / "local" / "tls" / "atlas-local-ca.crt"
    verify: Any = str(ca) if ca.is_file() else True

    def make() -> Any:
        return httpx.Client(timeout=30.0, verify=verify, follow_redirects=False)

    return SessionSend(make)
