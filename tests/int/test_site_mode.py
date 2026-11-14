"""The HTTPS site with built-in sign-in, wired as ``python -m atlas.serve --site`` wires it.

Runs in-process without TLS: the test client talks to the app through the site guard using the
site's own host name, so every rule the browser meets applies (host, origin, CSRF, sessions).
"""

from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from atlas.api.app import create_app
from atlas.api.routes_ops import _stream
from atlas.api.services import build_core
from atlas.api.streams import NOTIFICATION_STREAM
from atlas.config import ConfigError, load_settings
from atlas.housekeeping import Housekeeper, build_housekeeper
from atlas.identity import LocalIdentity, SessionResolver, UserDirectory, hash_password, login_router, write_users_file
from atlas.serve import _site_identity
from atlas.sessions import COOKIE_NAME, SessionStore
from atlas.site import SiteConfig
from atlas.wiring import connect

ORIGIN = "https://atlas.demo.internal"
PASSWORD = "correct horse battery staple"
CSRF_FIELD = re.compile(r'name="csrf" value="([^"]+)"')


def _users(path: Path) -> None:
    fast = dict(n=2 ** 10, r=8, p=1)  # fast hashes for tests only
    write_users_file(path, [
        {"name": "pm", "display_name": "Product manager (demo)", "password_hash": hash_password(PASSWORD, **fast),
         "roles": {"kubevirt": "pm", "argo-cd": "pm"}},
        {"name": "em", "display_name": "Engineering manager (demo)", "password_hash": hash_password(PASSWORD, **fast),
         "roles": {"kubevirt": "em"}},
    ])


@pytest.fixture()
def site(tmp_path):
    _users(tmp_path / "local" / "users.toml")
    settings = load_settings(None, environ={"ATLAS_DATA_DIR": str(tmp_path), "ATLAS_IDENTITY": "local",
                                            "ATLAS_DEFAULT_PROJECT": "kubevirt", "ATLAS_SANDBOX_RUNNER": "none",
                                            "ATLAS_PUBLISH_TARGET": "example/target"})
    cfg = SiteConfig(org_slug="demo", data_dir=tmp_path)
    sessions = SessionStore(cfg.session_db)
    provider = LocalIdentity(UserDirectory.load(tmp_path / "local" / "users.toml"), sessions,
                             default_project="kubevirt")
    services = build_core()
    services.default_project_id = "kubevirt"
    connect(services, settings, env={"ATLAS_SANDBOX_RUNNER": "none"}, persist=False)
    services.identity = SessionResolver(provider)
    app = create_app(services, site=cfg, sessions=sessions, extra_routers=(login_router(provider),))
    client = TestClient(app, base_url=ORIGIN, follow_redirects=False)
    return app, client, sessions, provider


def _sign_in(client: TestClient, user: str = "pm", password: str = PASSWORD) -> dict:
    page = client.get("/login")
    assert page.status_code == 200
    csrf = CSRF_FIELD.search(page.text).group(1)
    r = client.post("/login", data={"user": user, "password": password, "csrf": csrf}, headers={"Origin": ORIGIN})
    assert r.status_code == 303, r.text
    return client.get("/api/session").json()


def test_page_without_session_goes_to_sign_in(site):
    _, client, _, _ = site
    r = client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_sign_in_page_allows_only_its_own_form_action(site):
    _, client, _, _ = site
    assert "form-action 'self'" in client.get("/login").headers["content-security-policy"]
    assert "form-action 'none'" in client.get("/login.css").headers["content-security-policy"]


def test_sign_in_gives_a_session_with_projects_and_csrf(site):
    _, client, _, _ = site
    session = _sign_in(client)
    assert session["user"] == "pm" and session["projects"] == {"argo-cd": "pm", "kubevirt": "pm"}
    assert session["csrf_token"]
    assert client.get("/").status_code in (200, 404)  # the UI, or "not built" when ui/dist is absent


def test_wrong_password_is_refused_with_a_generic_message(site):
    _, client, _, _ = site
    csrf = CSRF_FIELD.search(client.get("/login").text).group(1)
    r = client.post("/login", data={"user": "pm", "password": "wrong", "csrf": csrf}, headers={"Origin": ORIGIN})
    assert r.status_code == 401 and "Sign-in failed" in r.text


def test_mutation_needs_origin_and_csrf(site):
    _, client, _, _ = site
    session = _sign_in(client)
    body = {"question": "Which components carry the most open flaky tests?"}
    key = {"Idempotency-Key": str(uuid.uuid4())}
    assert client.post("/api/triggers/question", json=body, headers=key).json()["error"] == "origin_required"
    no_csrf = client.post("/api/triggers/question", json=body, headers={**key, "Origin": ORIGIN})
    assert no_csrf.status_code == 403 and no_csrf.json()["error"] == "csrf_failed"
    ok = client.post("/api/triggers/question", json=body,
                     headers={**key, "Origin": ORIGIN, "X-CSRF-Token": session["csrf_token"]})
    # Past the site guard: any answer now comes from the API itself (here the trigger may be
    # unavailable because no model key is configured in the test), never a sign-in or CSRF refusal.
    assert ok.status_code != 401 and ok.json().get("error") not in {"csrf_failed", "origin_required", "unauthorised"}


def test_role_header_is_refused_in_sign_in_mode(site):
    _, client, _, _ = site
    assert client.get("/api/health", headers={"X-Atlas-Role": "audit"}).status_code == 401


def test_other_hosts_are_refused(site):
    app, _, _, _ = site
    other = TestClient(app, base_url="https://atlas.other.example", follow_redirects=False)
    assert other.get("/login").status_code == 421


def test_sign_out_ends_the_session(site):
    _, client, _, _ = site
    session = _sign_in(client)
    r = client.post("/logout", headers={"Origin": ORIGIN, "X-CSRF-Token": session["csrf_token"]})
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.get("/api/session").status_code == 401
    assert client.get("/api/notifications/stream", params={"follow": "false"}).status_code == 401


class _Request:
    """The parts of a request the stream reads."""

    def __init__(self, app, token: str) -> None:
        self.app = app
        self.cookies = {COOKIE_NAME: token}
        self.headers: dict[str, str] = {}
        self.path_params: dict[str, str] = {}
        self.query_params: dict[str, str] = {}

    async def is_disconnected(self) -> bool:
        return False


def test_open_stream_stops_after_sign_out(site):
    app, client, sessions, provider = site
    _sign_in(client)
    token = client.cookies.get(COOKIE_NAME)
    request = _Request(app, token)
    session = provider.session_of(request, touch=False)
    handle = sessions.bind_stream(session)
    app.state.services.notifier.emit("run.failed", "r1", ["r1"], frozenset({"public"}), {"text": "a run failed"})

    async def collect() -> list[str]:
        frames: list[str] = []
        loop = asyncio.get_running_loop()
        loop.call_later(0.5, provider.sign_out, token)  # sign out while the stream is open
        async for frame in _stream(request, NOTIFICATION_STREAM, None, None, True, handle):
            frames.append(frame)
        return frames

    frames = asyncio.run(asyncio.wait_for(collect(), timeout=10))
    assert handle.closed
    assert sessions.open_streams(session.session_id) == 0
    # The stream ended by itself after sign-out instead of waiting for the client to disconnect.


def test_site_identity_needs_local_mode_and_users(tmp_path):
    env = tmp_path / ".env"
    env.write_text(f"ATLAS_DATA_DIR={tmp_path.as_posix()}\nATLAS_IDENTITY=header\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="ATLAS_IDENTITY=local"):
        _site_identity(str(env), False)
    env.write_text(f"ATLAS_DATA_DIR={tmp_path.as_posix()}\nATLAS_IDENTITY=local\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="deploy.init"):
        _site_identity(str(env), False)
    _users(tmp_path / "local" / "users.toml")
    cfg, _, provider = _site_identity(str(env), True)
    assert cfg.host_name == "scenario.demo.internal" and isinstance(provider, LocalIdentity)


def test_housekeeper_runs_jobs_and_survives_a_failing_one():
    errors: list[str] = []

    def broken():
        raise RuntimeError("boom")

    keeper = Housekeeper({"ok": lambda: "done", "broken": broken}, on_error=lambda name, exc: errors.append(name))
    results = keeper.run_once()
    assert results["ok"] == "done" and results["broken"].startswith("failed") and errors == ["broken"]


def test_housekeeper_sweeps_approvals_and_sessions(site):
    app, _, sessions, _ = site
    keeper = build_housekeeper(app.state.services, sessions)
    assert set(keeper.jobs) == {"approvals", "sessions", "access"}
    results = keeper.run_once()
    assert set(results["approvals"]) == {"expired", "nearing_expiry", "sla_missed"}


def test_demo_runner_signs_in_per_role_with_csrf_and_origin(site):
    from demo.run_demo import Runner
    from demo.session_client import SessionSend

    app, _, sessions, _ = site
    made: list[TestClient] = []

    def make() -> TestClient:
        c = TestClient(app, base_url=ORIGIN, follow_redirects=False)
        made.append(c)
        return c

    send = SessionSend(make, password_for=lambda role: PASSWORD)
    status, _, health = send("GET", ORIGIN, "/api/health", {}, None, None)
    assert status == 200 and health["environment"] == "normal"
    body = {"question": "Which components carry the most open flaky tests?"}
    for role in ("pm", "em"):
        status, _, data = send("POST", ORIGIN, "/api/triggers/question",
                               {"X-Atlas-Role": role, "Idempotency-Key": str(uuid.uuid4())}, body, None)
        assert status != 401 and data.get("error") not in {"csrf_failed", "origin_required", "unauthorised"}, data
    assert len(made) == 2  # one signed-in client (cookie jar) per role on this server
    runner = Runner(send, {"normal": ORIGIN}, log=lambda _: None)
    runner.run_row({"row_id": "X", "title": "health", "shows": "health", "data_mode": "REAL", "server": "normal",
                    "calls": [{"method": "GET", "path": "/api/health", "role": "pm"}]})
