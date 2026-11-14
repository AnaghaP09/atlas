"""Sign-in routes and the HTTPS site guard, end to end through a small app."""

from __future__ import annotations

import re

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.testclient import TestClient

from atlas.identity import FailureLimiter, LocalIdentity, UserDirectory, hash_password, login_router, write_users_file
from atlas.sessions import COOKIE_NAME, SessionStore
from atlas.site import SiteConfig, SiteGuard

FAST = {"n": 2**10, "r": 8, "p": 1}


def build(tmp_path, scenario=False):
    path = tmp_path / "users.toml"
    write_users_file(path, [{"name": "pm", "display_name": "Product manager (demo)",
                             "password_hash": hash_password("right-pw", **FAST),
                             "roles": {"kubevirt": "pm"}}])
    sessions = SessionStore(tmp_path / ("s2.sqlite" if scenario else "s.sqlite"))
    provider = LocalIdentity(UserDirectory.load(path), sessions, default_project="kubevirt",
                             limiter=FailureLimiter())
    site = SiteConfig(org_slug="demo", scenario=scenario, data_dir=tmp_path)
    app = FastAPI()
    app.include_router(login_router(provider))

    @app.get("/api/health")
    async def health():
        return {"ok": True}

    @app.post("/api/things")
    async def things():
        return {"done": True}

    @app.get("/")
    async def home():
        return HTMLResponse("<p>home</p>")

    @app.get("/api/whoami")
    async def whoami(request: Request):
        principal = provider.authenticate(request)
        return JSONResponse({"role": principal.role.value if principal else None})

    guarded = SiteGuard(app, site, sessions)
    client = TestClient(guarded, base_url=site.origin)
    return client, site, provider


def sign_in(client, site, password="right-pw"):
    page = client.get("/login")
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    return client.post("/login", data={"user": "pm", "password": password, "csrf": csrf},
                       headers={"origin": site.origin}, follow_redirects=False)


def test_page_without_session_goes_to_sign_in(tmp_path):
    client, _, _ = build(tmp_path)
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.get("/login").status_code == 200


def test_sign_in_sets_secure_session_cookie_and_reaches_home(tmp_path):
    client, site, _ = build(tmp_path)
    r = sign_in(client, site)
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = [c for c in r.headers.get_list("set-cookie") if c.startswith(COOKIE_NAME)][0]
    for part in ("Secure", "HttpOnly", "SameSite=Strict", "Path=/"):
        assert part in cookie
    assert client.get("/").status_code == 200
    assert client.get("/api/whoami").json() == {"role": "pm"}


def test_wrong_password_shows_generic_message(tmp_path):
    client, site, _ = build(tmp_path)
    r = sign_in(client, site, password="nope")
    assert r.status_code == 401 and "Sign-in failed" in r.text
    assert COOKIE_NAME not in r.headers.get("set-cookie", "")


def test_sign_in_form_needs_its_csrf_token(tmp_path):
    client, site, _ = build(tmp_path)
    client.get("/login")
    r = client.post("/login", data={"user": "pm", "password": "right-pw", "csrf": "forged"},
                    headers={"origin": site.origin}, follow_redirects=False)
    assert r.status_code == 400


def test_mutations_need_origin_and_csrf(tmp_path):
    client, site, _ = build(tmp_path)
    sign_in(client, site)
    csrf = client.get("/api/session").json()["csrf_token"]
    assert client.post("/api/things").status_code == 403  # no Origin
    assert client.post("/api/things", headers={"origin": site.origin}).status_code == 403  # no CSRF
    assert client.post("/api/things", headers={"origin": site.origin, "x-csrf-token": "x"}).status_code == 403
    ok = client.post("/api/things", headers={"origin": site.origin, "x-csrf-token": csrf})
    assert ok.status_code == 200


def test_foreign_origin_is_refused_even_for_reads(tmp_path):
    client, _, _ = build(tmp_path)
    assert client.get("/login", headers={"origin": "https://evil.example"}).status_code == 403


@pytest.mark.parametrize("host", ["evil.example", "atlas.other.internal", "localhost:443", "rebind.attacker.test"])
def test_other_hosts_are_refused(tmp_path, host):
    client, _, _ = build(tmp_path)
    assert client.get("/api/health", headers={"host": host}).status_code == 421


def test_loopback_address_only_answers_the_health_probe(tmp_path):
    client, _, _ = build(tmp_path)
    assert client.get("/api/health", headers={"host": "127.0.0.1:443"}).status_code == 200
    assert client.get("/login", headers={"host": "127.0.0.1:443"}).status_code == 421
    assert client.get("/api/session", headers={"host": "127.0.0.1:443"}).status_code == 421


def test_role_header_is_refused(tmp_path):
    client, site, _ = build(tmp_path)
    sign_in(client, site)
    assert client.get("/api/whoami", headers={"x-atlas-role": "audit"}).status_code == 401


def test_sign_out_needs_csrf_and_ends_session_and_streams(tmp_path):
    client, site, provider = build(tmp_path)
    sign_in(client, site)
    token = client.cookies.get(COOKIE_NAME)
    session = provider.sessions.validate(token, touch=False)
    handle = provider.sessions.bind_stream(session)
    bad = client.post("/logout", headers={"origin": site.origin, "x-csrf-token": "wrong"}, follow_redirects=False)
    assert bad.status_code == 403 and not handle.closed
    good = client.post("/logout", headers={"origin": site.origin, "x-csrf-token": session.csrf_token},
                       follow_redirects=False)
    assert good.status_code == 303 and good.headers["location"] == "/login"
    assert handle.closed
    assert provider.sessions.validate(token) is None


def test_session_endpoint_reports_projects_without_secrets(tmp_path):
    client, site, _ = build(tmp_path)
    sign_in(client, site)
    body = client.get("/api/session").json()
    assert body["user"] == "pm" and body["projects"] == {"kubevirt": "pm"}
    assert "password" not in str(body).lower()


def test_scenario_server_uses_its_own_host_and_origin(tmp_path):
    client, site, _ = build(tmp_path, scenario=True)
    assert site.origin == "https://scenario.demo.internal:8443"
    assert site.allowed_hosts == frozenset({"scenario.demo.internal:8443"})
    assert client.get("/login").status_code == 200
    assert client.get("/login", headers={"host": "atlas.demo.internal"}).status_code == 421
    r = sign_in(client, site)
    assert r.status_code == 303
    assert client.post("/api/things", headers={"origin": "https://atlas.demo.internal"}).status_code == 403


def test_main_site_names_and_tls_options(tmp_path):
    site = SiteConfig(org_slug="acme", data_dir=tmp_path)
    assert site.host_name == "atlas.acme.internal" and site.origin == "https://atlas.acme.internal"
    assert site.allowed_hosts == frozenset({"atlas.acme.internal", "atlas.acme.internal:443"})
    with pytest.raises(FileNotFoundError):
        site.uvicorn_options()
    site.tls_dir.mkdir(parents=True)
    site.cert_file.write_text("c")
    site.key_file.write_text("k")
    options = site.uvicorn_options()
    assert options["host"] == "127.0.0.1" and options["port"] == 443 and options["ssl_certfile"].endswith("server.crt")
