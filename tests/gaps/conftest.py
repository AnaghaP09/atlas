"""A server wired to the real layers, with one configured project and no network."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from atlas.api.app import create_app
from atlas.api.services import build_core
from atlas.config import load_settings
from atlas.gateway import Response
from atlas.housekeeping import Housekeeper
from atlas.watchers import housekeeping_watcher
from atlas.wiring import connect

BASE = "http://127.0.0.1:8000"
PROJECTS = """
[projects.alpha]
repository = "example/alpha"
ci = "github_actions"
[projects.alpha.labels]
bug = "bug"
"""


def _no_network(method, url, headers, body):
    return Response(503, None, {})


@pytest.fixture()
def server(tmp_path):
    (tmp_path / "local").mkdir()
    (tmp_path / "local" / "projects.toml").write_text(PROJECTS, encoding="utf-8")
    env = {"ATLAS_DATA_DIR": str(tmp_path), "ATLAS_SANDBOX_RUNNER": "none", "ATLAS_DEFAULT_PROJECT": "alpha"}
    settings = load_settings(None, environ=env)
    services = build_core()
    services.default_project_id = "alpha"
    connect(services, settings, env=env, data_transport=_no_network)
    sweeps: list[str] = []
    services.scheduler.add(housekeeping_watcher(Housekeeper({"sweep": lambda: sweeps.append("ran")}).run_once, 300))
    client = TestClient(create_app(services), base_url=BASE)
    return services, client, sweeps


def headers(role: str) -> dict[str, str]:
    return {"X-Atlas-Role": role, "Idempotency-Key": str(uuid.uuid4()), "Origin": BASE}
