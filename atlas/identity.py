"""Built-in sign-in for the customer view.

People sign in with a user name and password on an ATLAS page. There is no outside identity
provider, so sign-in adds no outbound network path.

- The user list lives in one file, ``users.toml``, in the private data directory. It holds each
  person's display name, a scrypt password hash (never the password) and their role in each
  project. It is the only source of who may do what.
- Five failed attempts for a user within 15 minutes block that user for the rest of the window.
  Every failure shows the same message, and an unknown user takes the same time as a wrong
  password.
- A signed-in request is identified only by the session cookie. A request that also carries the
  demo role header is refused, so the header cannot be used to pick a role.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import secrets
import threading
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from atlas.contracts import IdentityMode, Principal, Role
from atlas.projection import principal_for
from atlas.sessions import COOKIE_NAME, Session, SessionStore, clear_cookie, session_cookie

ROLE_HEADER = "x-atlas-role"
CSRF_HEADER = "x-csrf-token"
LOGIN_COOKIE = "__Host-atlas_login"
GENERIC_FAILURE = "Sign-in failed. Check your user name and password, then try again."

# scrypt cost: about 16 MB of memory and tens of milliseconds per check.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1
MAX_FAILURES = 5
FAILURE_WINDOW_SECONDS = 15 * 60


# ---------------------------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------------------------

def hash_password(password: str, *, n: int = SCRYPT_N, r: int = SCRYPT_R, p: int = SCRYPT_P) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32, maxmem=64 * 1024 * 1024)
    return "$".join(["scrypt", str(n), str(r), str(p),
                     base64.b64encode(salt).decode(), base64.b64encode(digest).decode()])


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=int(n), r=int(r), p=int(p),
                                dklen=len(expected), maxmem=64 * 1024 * 1024)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


# ---------------------------------------------------------------------------------------------
# Users and memberships
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class User:
    name: str
    display_name: str
    password_hash: str
    roles: Mapping[str, Role]  # project id -> role in that project


class UserDirectoryError(ValueError):
    pass


@dataclass
class UserDirectory:
    users: dict[str, User] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | str) -> "UserDirectory":
        path = Path(path)
        if not path.is_file():
            raise UserDirectoryError(f"user file not found: {path.name}; run the setup step first")
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        users: dict[str, User] = {}
        for name, entry in (data.get("users") or {}).items():
            roles: dict[str, Role] = {}
            for project, role in (entry.get("roles") or {}).items():
                try:
                    roles[project] = Role(role)
                except ValueError as exc:
                    raise UserDirectoryError(f"user {name}: unknown role {role!r} in project {project!r}") from exc
            if not entry.get("password_hash"):
                raise UserDirectoryError(f"user {name}: missing password_hash")
            users[name] = User(name, entry.get("display_name", name), entry["password_hash"], roles)
        return cls(users)

    def get(self, name: str) -> User | None:
        return self.users.get(name)

    def role_in(self, name: str, project: str) -> Role | None:
        user = self.users.get(name)
        return None if user is None else user.roles.get(project)

    def projects_of(self, name: str) -> list[str]:
        user = self.users.get(name)
        return sorted(user.roles) if user else []


def write_users_file(path: Path | str, entries: list[dict[str, Any]]) -> None:
    """Write users.toml. Each entry: name, display_name, password_hash, roles {project: role}."""

    def q(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    lines = ["# ATLAS sign-in users. Holds password hashes only. Keep this file private.", ""]
    for entry in entries:
        lines.append(f"[users.{q(entry['name'])}]")
        lines.append(f"display_name = {q(entry['display_name'])}")
        lines.append(f"password_hash = {q(entry['password_hash'])}")
        lines.append(f"[users.{q(entry['name'])}.roles]")
        for project, role in sorted(entry["roles"].items()):
            lines.append(f"{q(project)} = {q(role)}")
        lines.append("")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------------------------
# Rate limit
# ---------------------------------------------------------------------------------------------

class FailureLimiter:
    def __init__(self, clock: Callable[[], float] = time.time, max_failures: int = MAX_FAILURES,
                 window_seconds: int = FAILURE_WINDOW_SECONDS) -> None:
        self._clock = clock
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str) -> list[float]:
        cutoff = self._clock() - self.window_seconds
        recent = [t for t in self._failures.get(key, []) if t > cutoff]
        self._failures[key] = recent
        return recent

    def blocked(self, user: str) -> bool:
        with self._lock:
            return len(self._recent(user.lower())) >= self.max_failures

    def record_failure(self, user: str) -> None:
        with self._lock:
            self._recent(user.lower()).append(self._clock())

    def reset(self, user: str) -> None:
        with self._lock:
            self._failures.pop(user.lower(), None)


# ---------------------------------------------------------------------------------------------
# The identity provider
# ---------------------------------------------------------------------------------------------

class SignInRefused(Exception):
    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


@dataclass
class SignInResult:
    ok: bool
    token: str | None = None
    session: Session | None = None
    message: str = ""


class LocalIdentity:
    """Built-in sign-in. Implements the shared IdentityProvider interface."""

    mode = IdentityMode.LOCAL

    def __init__(self, users: UserDirectory, sessions: SessionStore, *, default_project: str = "",
                 limiter: FailureLimiter | None = None) -> None:
        self.users = users
        self.sessions = sessions
        self.default_project = default_project
        self.limiter = limiter or FailureLimiter()

    # -- requests -----------------------------------------------------------------------------

    @staticmethod
    def _header(request: Any, name: str) -> str | None:
        return request.headers.get(name)

    @staticmethod
    def _cookie(request: Any, name: str) -> str | None:
        cookies = getattr(request, "cookies", None) or {}
        return cookies.get(name)

    def session_of(self, request: Any, *, touch: bool = True) -> Session | None:
        if self._header(request, ROLE_HEADER) is not None:
            raise SignInRefused(401, "the demo role header is not accepted when sign-in is on")
        return self.sessions.validate(self._cookie(request, COOKIE_NAME), touch=touch)

    def project_for(self, request: Any, user: str) -> str | None:
        """The project a request acts in: the requested one if the user belongs to it."""
        requested = None
        path_params = getattr(request, "path_params", None) or {}
        requested = path_params.get("project_id") or request.query_params.get("project_id")
        projects = self.users.projects_of(user)
        if requested:
            return requested if requested in projects else None
        if self.default_project in projects:
            return self.default_project
        return projects[0] if projects else None

    def authenticate(self, request: Any) -> Principal | None:
        session = self.session_of(request)
        if session is None:
            return None
        project = self.project_for(request, session.user)
        if project is None:
            raise SignInRefused(403, "you are not a member of this project")
        role = self.users.role_in(session.user, project)
        if role is None:
            raise SignInRefused(403, "you have no role in this project")
        return principal_for(role.value, project_id=project, user=session.user)

    # -- sign-in and sign-out -----------------------------------------------------------------

    def sign_in(self, user_name: str, password: str, previous_token: str | None = None) -> SignInResult:
        user_name = (user_name or "").strip()
        user = self.users.get(user_name)
        if self.limiter.blocked(user_name):
            verify_password(password or "", _DUMMY_HASH)  # same work as a real check
            return SignInResult(False, message=GENERIC_FAILURE)
        ok = verify_password(password or "", user.password_hash if user else _DUMMY_HASH) and user is not None
        if not ok or not user.roles:
            self.limiter.record_failure(user_name)
            return SignInResult(False, message=GENERIC_FAILURE)
        self.limiter.reset(user_name)
        if previous_token:
            self.sessions.revoke(previous_token)  # rotation: never reuse a token seen before sign-in
        token, session = self.sessions.create(user.name)
        return SignInResult(True, token=token, session=session)

    def sign_out(self, token: str | None) -> None:
        if token:
            self.sessions.revoke(token)

    # IdentityProvider methods that take a framework request are provided by the routes below.
    def login(self, request: Any) -> Any:  # pragma: no cover - the routes call sign_in directly
        raise NotImplementedError("use login_router(); it reads the form and calls sign_in")

    def logout(self, request: Any) -> Any:  # pragma: no cover - the routes call sign_out directly
        raise NotImplementedError("use login_router(); it checks CSRF and calls sign_out")


class SessionResolver:
    """Adapter for the API's single principal-resolution point (``app.state.identity``)."""

    mode = "local"

    def __init__(self, provider: LocalIdentity) -> None:
        self.provider = provider

    def resolve(self, request: Any) -> Principal | None:
        from atlas.api.identity import IdentityError

        try:
            return self.provider.authenticate(request)
        except SignInRefused as exc:
            raise IdentityError(exc.status, exc.reason) from exc


# ---------------------------------------------------------------------------------------------
# Routes: sign-in page, sign-in, sign-out, current session
# ---------------------------------------------------------------------------------------------

_LOGIN_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in - ATLAS</title>
<link rel="stylesheet" href="/login.css">
</head>
<body>
<main class="card">
<h1>ATLAS</h1>
<p class="sub">Sign in to continue</p>
{message}
<form method="post" action="/login">
<input type="hidden" name="csrf" value="{csrf}">
<label for="user">User name</label>
<input id="user" name="user" autocomplete="username" required>
<label for="password">Password</label>
<input id="password" name="password" type="password" autocomplete="current-password" required>
<button type="submit">Sign in</button>
</form>
<p class="note">{note}</p>
</main>
</body>
</html>
"""

_LOGIN_CSS = """
body{margin:0;font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;display:flex;
min-height:100vh;align-items:center;justify-content:center}
.card{background:#1e293b;padding:2rem;border-radius:12px;width:min(22rem,90vw)}
h1{margin:0;letter-spacing:.2em}.sub{color:#94a3b8;margin:.25rem 0 1.5rem}
label{display:block;margin:.75rem 0 .25rem;font-size:.9rem}
input{width:100%;box-sizing:border-box;padding:.6rem;border-radius:6px;border:1px solid #475569;
background:#0f172a;color:#e2e8f0}
button{margin-top:1.25rem;width:100%;padding:.7rem;border:0;border-radius:6px;background:#38bdf8;
color:#0f172a;font-weight:600;cursor:pointer}
.error{background:#7f1d1d;padding:.6rem;border-radius:6px}.note{color:#94a3b8;font-size:.8rem;margin-top:1.5rem}
"""


def login_router(provider: LocalIdentity, *, note: str = "") -> Any:
    """FastAPI routes for sign-in. Mounted at the site root, outside /api."""
    router = APIRouter()

    async def _form(request: Request) -> dict[str, str]:
        """Read a small URL-encoded form without extra packages."""
        if not request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
            return {}
        body = await request.body()
        if len(body) > 8192:
            return {}
        return {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True).items()}

    def _page(csrf: str, message: str = "", status: int = 200) -> HTMLResponse:
        body = _LOGIN_PAGE.format(
            csrf=html.escape(csrf),
            message=f'<p class="error" role="alert">{html.escape(message)}</p>' if message else "",
            note=html.escape(note),
        )
        response = HTMLResponse(body, status_code=status, headers={"Cache-Control": "no-store"})
        response.headers.append("Set-Cookie", f"{LOGIN_COOKIE}={csrf}; Path=/; Max-Age=900; Secure; HttpOnly; SameSite=Strict")
        return response

    @router.get("/login.css", include_in_schema=False)
    async def login_css() -> Response:
        return Response(_LOGIN_CSS, media_type="text/css")

    @router.get("/login", include_in_schema=False)
    async def login_page() -> HTMLResponse:
        return _page(secrets.token_urlsafe(32))

    @router.post("/login", include_in_schema=False)
    async def login_submit(request: Request) -> Response:
        form = await _form(request)
        user, password, csrf = form.get("user", ""), form.get("password", ""), form.get("csrf", "")
        cookie_csrf = request.cookies.get(LOGIN_COOKIE, "")
        if not cookie_csrf or not csrf or not hmac.compare_digest(cookie_csrf.encode(), csrf.encode()):
            return _page(secrets.token_urlsafe(32), "Your sign-in form expired. Please try again.", 400)
        if request.headers.get(ROLE_HEADER) is not None:
            return _page(secrets.token_urlsafe(32), GENERIC_FAILURE, 400)
        result = provider.sign_in(user, password, previous_token=request.cookies.get(COOKIE_NAME))
        if not result.ok:
            return _page(secrets.token_urlsafe(32), result.message, 401)
        response = RedirectResponse("/", status_code=303)
        response.headers.append("Set-Cookie", session_cookie(result.token or ""))
        response.headers.append("Set-Cookie", f"{LOGIN_COOKIE}=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Strict")
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.post("/logout", include_in_schema=False)
    async def logout(request: Request) -> Response:
        token = request.cookies.get(COOKIE_NAME)
        session = provider.sessions.validate(token, touch=False)
        presented = request.headers.get(CSRF_HEADER)
        if presented is None:
            presented = (await _form(request)).get("csrf", "")
        if session is not None and not session.csrf_ok(presented):
            return JSONResponse({"error": "forbidden", "reason": "missing or wrong CSRF token"}, status_code=403)
        provider.sign_out(token)
        response = RedirectResponse("/login", status_code=303)
        response.headers.append("Set-Cookie", clear_cookie())
        return response

    @router.get("/api/session", tags=["session"])
    async def current_session(request: Request) -> JSONResponse:
        try:
            session = provider.session_of(request)
        except SignInRefused as exc:
            return JSONResponse({"error": "unauthorised", "reason": exc.reason}, status_code=exc.status)
        if session is None:
            return JSONResponse({"error": "unauthorised", "reason": "sign-in required"}, status_code=401)
        user = provider.users.get(session.user)
        return JSONResponse({
            "user": session.user,
            "display_name": user.display_name if user else session.user,
            "projects": {p: r.value for p, r in (user.roles.items() if user else [])},
            "default_project": provider.project_for(request, session.user),
            "csrf_token": session.csrf_token,
            "expires_at": session.created_at + provider.sessions.absolute_seconds,
            "idle_seconds": provider.sessions.idle_seconds,
        }, headers={"Cache-Control": "no-store"})

    return router
