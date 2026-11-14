"""Route conventions: /api prefix, Host and Origin checks, identity, envelope, projection, errors."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from atlas.api.app import create_app
from atlas.api.services import build_core
from atlas.config import ConfigError, load_settings
from tests.d.conftest import BASE, H


def test_every_route_is_under_api(app):
    paths = app.openapi()["paths"]
    assert paths, "no routes"
    assert all(p.startswith("/api/") for p in paths)


def test_openapi_at_api_prefix(client):
    assert client.get("/api/openapi.json").status_code == 200


def test_unknown_api_path_is_json_404_not_ui(client):
    r = client.get("/api/does-not-exist", headers=H("pm"))
    assert r.status_code == 404
    assert r.json() == {"error": "not_found", "reason": "no such route"}


@pytest.mark.parametrize("host", ["evil.example:8000", "attacker.example", "127.0.0.1:9999", "localhost:80"])
def test_host_allow_list_rejects(app, host):
    c = TestClient(app, base_url=BASE)
    r = c.get("/api/health", headers={"Host": host})
    assert r.status_code == 421
    assert r.json()["error"] == "host_not_allowed"


@pytest.mark.parametrize("host", ["127.0.0.1:8000", "localhost:8000", "[::1]:8000"])
def test_host_allow_list_accepts(app, host):
    c = TestClient(app, base_url=BASE)
    assert c.get("/api/health", headers={"Host": host}).status_code == 200


def test_rebinding_style_request_on_loopback_is_rejected(app):
    # A page served from attacker.example whose name now resolves to 127.0.0.1.
    c = TestClient(app, base_url=BASE)
    r = c.get("/api/runs", headers={"Host": "attacker.example:8000", "Origin": "http://attacker.example:8000",
                                     "X-Atlas-Role": "audit"})
    assert r.status_code == 421


def test_origin_rules(client):
    assert client.get("/api/runs", headers={"Origin": "http://evil.example", "X-Atlas-Role": "pm"}).status_code == 403
    assert client.get("/api/runs", headers={"Origin": "http://127.0.0.1:8000", "X-Atlas-Role": "pm"}).status_code == 200
    assert client.get("/api/runs", headers={"X-Atlas-Role": "pm"}).status_code == 200  # no Origin: non-browser


def test_identity_required_and_validated(client):
    assert client.get("/api/runs").status_code == 401
    assert client.get("/api/runs", headers={"X-Atlas-Role": "ceo"}).status_code == 401


def test_query_parameters_cannot_widen_the_reader(client, services):
    services.snapshots.records[("snap-v1.9.0", "rec-restricted")]  # exists
    r = client.post("/api/triggers/failure", json={"snapshot_id": "snap-v1.9.0", "event_id": "G04"}, headers=H("em"))
    run_id = r.json()["run_id"]
    for extra in ({"reader": "security"}, {"role": "audit"}, {"principal": "audit"}):
        r = client.get("/api/evidence/rec-restricted", params={"run_id": run_id, **extra},
                       headers={"X-Atlas-Role": "pm"})
        assert r.status_code == 200
        assert "SECRET-FINDING-MARKER" not in r.text
        assert r.json()["record"] is None and r.json()["restricted_items"] >= 1


def test_non_loopback_bind_refused_with_header_identity(tmp_path):
    env = tmp_path / ".env"
    env.write_text("ATLAS_HOST=0.0.0.0\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings(env, environ={})


ENVELOPE = {"data_mode", "environment", "server_time"}


def _get_routes(app) -> list[str]:
    out = []
    for path, ops in app.openapi()["paths"].items():
        if "get" in ops and "stream" not in path:
            out.append(path)
    return out


def test_every_get_route_returns_the_envelope_and_never_leaks(client, services):
    run_id = client.post("/api/triggers/failure", json={"snapshot_id": "snap-v1.9.0", "event_id": "G04"},
                         headers=H("em")).json()["run_id"]
    task = client.post("/api/tasks", json={"run_id": run_id}, headers=H("em"))
    assert task.status_code == 409  # the stub run assigns a developer, nothing to delegate
    services.containment.trip("publication", "test")
    client.post("/api/evaluations", headers=H("pm"))  # not a route; ignored
    services.evaluations.add({"gates": {"G-1": {"status": "passed"}},
                              "excerpt": {"text": "SECRET-FINDING-MARKER", "required_scopes": ["restricted:security"]}},
                             code_revision="abc", corpus={"cases": ["G01"]})
    fill = {"snapshot_id": "snap-v1.9.0", "run_id": run_id, "report_id": f"{run_id}:investigation",
            "record_id": "rec-restricted", "evaluation_id": services.evaluations.list()[0]["evaluation_id"],
            "task_id": "missing", "switch_id": "publication", "correction_id": "missing"}
    params = {"run_id": run_id, "incident_id": "x"}
    checked = 0
    for path in _get_routes(client.app):
        url = re.sub(r"\{(\w+)\}", lambda m: fill.get(m.group(1), "x"), path)
        r = client.get(url, params={k: v for k, v in params.items() if k in path or k in ("run_id", "incident_id")},
                       headers={"X-Atlas-Role": "pm"})
        assert "SECRET-FINDING-MARKER" not in r.text, url
        if r.status_code == 200:
            assert ENVELOPE <= set(r.json()), url
            checked += 1
    assert checked >= 15


def test_errors_use_one_schema(client):
    r = client.post("/api/triggers/failure", json={"snapshot_id": "snap-v1.9.0"}, headers=H("em"))
    assert r.status_code == 400 and set(r.json()) >= {"error", "reason"}


def test_security_headers_on_ui_routes(client):
    r = client.get("/", headers={"Host": "127.0.0.1:8000"})
    assert "frame-ancestors 'none'" in r.headers.get("content-security-policy", "")
    assert r.headers["content-type"].startswith("application/json")  # no UI built: JSON, never an api route


def test_staged_and_unevaluated_listed_in_health(client):
    body = client.get("/api/health").json()
    assert body["staged"] and "environment" in body and "configuration" in body


def test_health_never_reveals_secret_values(tmp_path, monkeypatch):
    from atlas.api.__main__ import build_server

    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=sk-test-not-a-real-key-123456\nATLAS_DATA_DIR=" + str(tmp_path).replace("\\", "/") + "\n",
                   encoding="utf-8")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _, app = build_server(str(env), scenario=False)
    r = TestClient(app, base_url=BASE).get("/api/health")
    assert r.json()["configuration"]["ANTHROPIC_API_KEY"] is True
    assert "sk-test" not in r.text


def test_scenario_routes_absent_on_normal_server(client):
    paths = client.app.openapi()["paths"]
    assert not any("/scenario/" in p for p in paths)
    assert client.post("/api/scenario/faults", json={}, headers=H("audit")).status_code in (404, 405)


def test_scenario_server_refuses_without_its_flag(tmp_path):
    from atlas.api.__main__ import build_server

    env = tmp_path / ".env.scenario"
    env.write_text("ATLAS_SERVER_ROLE=normal\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        build_server(str(env), scenario=True)


def test_scenario_server_has_scenario_routes_and_own_store():
    s = build_core(environment="scenario", port=8001)
    app = create_app(s)
    assert any("/scenario/faults" in p for p in app.openapi()["paths"])
    c = TestClient(app, base_url="http://127.0.0.1:8001")
    r = c.post("/api/scenario/faults", json={"fault": "connector_failure"}, headers=H("audit"))
    assert r.status_code == 201 and r.json()["scenario"]["provenance"] == "synthetic"
    assert r.json()["environment"] == "scenario"
    assert c.post("/api/scenario/faults", json={"fault": "x"}, headers=H("em")).status_code == 403
