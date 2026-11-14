"""The server uses the real data layer when it has a data directory."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from atlas.api.app import create_app
from atlas.api.services import build_core
from atlas.config import load_settings
from atlas.data_wiring import DataSnapshots
from atlas.gateway import Response
from atlas.wiring import connect

BASE = "http://127.0.0.1:8000"
PROJECTS = """
[projects.alpha]
repository = "example/alpha"
ci = "github_actions"
[projects.alpha.labels]
bug = "bug"
[projects.beta]
repository = "example/beta"
"""


def _offline_transport(method, url, headers, body):
    return Response(503, None, {})


def test_server_connects_the_data_layer_per_project(tmp_path):
    (tmp_path / "local").mkdir()
    (tmp_path / "local" / "projects.toml").write_text(PROJECTS, encoding="utf-8")
    settings = load_settings(None, environ={"ATLAS_DATA_DIR": str(tmp_path), "ATLAS_SANDBOX_RUNNER": "none",
                                            "ATLAS_DEFAULT_PROJECT": "alpha"})
    services = build_core()
    services.default_project_id = "alpha"
    wiring = connect(services, settings, env={"ATLAS_SANDBOX_RUNNER": "none", "ATLAS_DASHBOARD_POLL": "off"},
                     data_transport=_offline_transport)
    assert wiring.data_layer == "connected"
    assert isinstance(services.snapshots, DataSnapshots)
    assert not any("evidence data" in s for s in services.staged)
    data = wiring.extra["data"]
    assert set(data.pollers) == {"alpha", "beta"}
    assert data.poll_loop._thread is None  # polling switched off for this test

    client = TestClient(create_app(services), base_url=BASE)
    key = {"X-Atlas-Role": "em", "Idempotency-Key": str(uuid.uuid4())}
    snap = client.post("/api/snapshots", headers=key, json={}).json()
    sid = snap["snapshot_id"]
    assert sid and sid.startswith("alpha.")
    listing = client.get("/api/snapshots", headers={"X-Atlas-Role": "pm"}).json()
    assert sid in str(listing)
