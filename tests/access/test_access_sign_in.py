"""The access-request workflow end to end on the sign-in site, with separate signed-in people.

The app runs behind the site guard with real sessions, cookies, Origin and CSRF checks; the data and
execution layers are the test stand-ins.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from atlas.api.app import create_app
from atlas.api.services import build_core
from atlas.identity import LocalIdentity, SessionResolver, UserDirectory, hash_password, login_router, write_users_file
from atlas.sessions import SessionStore
from atlas.site import SiteConfig
from tests.d.conftest import StubOrchestrator
from tests.d.fakes import FakeContainmentPort, FakeSnapshots, FakeTasks

ORIGIN = "https://atlas.demo.internal"
PASSWORD = "correct horse battery staple"
CSRF_FIELD = re.compile(r'name="csrf" value="([^"]+)"')


class Person:
    """One signed-in browser: its own cookie jar and CSRF token."""

    def __init__(self, app, user: str) -> None:
        self.client = TestClient(app, base_url=ORIGIN, follow_redirects=False)
        page = self.client.get("/login")
        csrf = CSRF_FIELD.search(page.text).group(1)
        r = self.client.post("/login", data={"user": user, "password": PASSWORD, "csrf": csrf},
                             headers={"Origin": ORIGIN})
        assert r.status_code == 303, r.text
        self.session = self.client.get("/api/session").json()

    def get(self, path: str, **params):
        return self.client.get(path, params=params or None)

    def post(self, path: str, body: dict | None = None):
        return self.client.post(path, json=body or {}, headers={
            "Origin": ORIGIN, "X-CSRF-Token": self.session["csrf_token"], "Idempotency-Key": str(uuid.uuid4())})


@pytest.fixture()
def site(tmp_path: Path):
    fast = dict(n=2 ** 10, r=8, p=1)  # fast hashes for tests only
    users = [{"name": name, "display_name": f"{name} (demo)", "password_hash": hash_password(PASSWORD, **fast),
              "roles": roles}
             for name, roles in (("pm", {"kubevirt": "pm"}), ("pm-two", {"kubevirt": "pm"}),
                                 ("em", {"kubevirt": "em"}), ("security", {"kubevirt": "security"}),
                                 ("outsider", {"argo-cd": "em"}))]
    write_users_file(tmp_path / "local" / "users.toml", users)
    cfg = SiteConfig(org_slug="demo", data_dir=tmp_path)
    sessions = SessionStore(cfg.session_db)
    provider = LocalIdentity(UserDirectory.load(tmp_path / "local" / "users.toml"), sessions,
                             default_project="kubevirt")
    services = build_core()
    services.default_project_id = "kubevirt"
    services.snapshots, services.tasks, services.containment = FakeSnapshots(), FakeTasks(), FakeContainmentPort()
    services.model_configured = True
    services.orchestrator = StubOrchestrator(services)
    services.orchestrator.action = "DELEGATE_TO_CODING_AGENT"
    services.identity = SessionResolver(provider)
    app = create_app(services, site=cfg, sessions=sessions, extra_routers=(login_router(provider),))
    return app, services


def _seed(em: Person, services):
    run = em.post("/api/triggers/manual/investigate", {"snapshot_id": "snap-v1.9.0", "event_id": "G01"})
    assert run.status_code == 202, run.text
    run_id = run.json()["run_id"]
    task = em.post("/api/tasks", {"run_id": run_id})
    assert task.status_code == 201, task.text
    return run_id, task.json()["task"]["task_id"], next(iter(services.tasks.approval_rows))


def test_ordinary_item_granted_at_once_then_revoked_by_the_owner(site):
    app, services = site
    em, pm = Person(app, "em"), Person(app, "pm")
    _, task_id, approval_id = _seed(em, services)

    assert pm.get(f"/api/approvals/{approval_id}").status_code == 403
    asked = pm.post("/api/access-requests", {"object_type": "approval", "object_id": approval_id})
    assert asked.status_code == 201 and asked.json()["access"] == "open"
    req = asked.json()["request"]
    assert req["requester"] == {"user": "pm", "role": "pm"} and req["grant"]["automatic"]

    assert pm.get(f"/api/approvals/{approval_id}").json()["approval"]["task_id"] == task_id
    # the grant belongs to that person, not to the role
    assert Person(app, "pm-two").get(f"/api/approvals/{approval_id}").status_code == 403
    # read-only: every change is still refused
    assert pm.post(f"/api/approvals/{approval_id}/decision", {"decision": "approve"}).status_code == 403
    assert pm.post(f"/api/tasks/{task_id}/prepare").status_code == 403
    assert pm.post(f"/api/tasks/{task_id}/publish").status_code == 403

    notes = em.get("/api/notifications").json()["notifications"]
    assert any(n["event_type"] == "access.granted_automatically" for n in notes)
    revoked = em.post(f"/api/access-grants/{req['grant']['grant_id']}/revoke")
    assert revoked.status_code == 200 and revoked.json()["request"]["status"] == "revoked"
    assert pm.get(f"/api/approvals/{approval_id}").status_code == 403
    stream = pm.get("/api/notifications/stream", follow="false").text
    assert "access.revoked" in stream and approval_id not in stream


def test_sensitive_item_needs_the_security_role_and_expires(site):
    app, services = site
    em, pm, security = Person(app, "em"), Person(app, "pm"), Person(app, "security")
    run_id, _, _ = _seed(em, services)
    finding = f"{run_id}:1"

    asked = pm.post("/api/access-requests", {"object_type": "finding", "object_id": finding})
    assert asked.status_code == 201 and asked.json()["access"] == "locked"
    rid = asked.json()["request"]["request_id"]
    assert asked.json()["request"]["status"] == "pending"
    assert pm.get(f"/api/objects/finding/{finding}").status_code == 403

    assert em.post(f"/api/access-requests/{rid}/decision", {"decision": "grant"}).status_code == 403
    own = pm.post(f"/api/access-requests/{rid}/decision", {"decision": "grant"})
    assert own.status_code == 403 and "own request" in own.json()["reason"]
    granted = security.post(f"/api/access-requests/{rid}/decision", {"decision": "grant", "hours": 1})
    assert granted.status_code == 200
    assert granted.json()["request"]["grant"]["granted_by"] == {"user": "security", "role": "security"}

    read = pm.get(f"/api/objects/finding/{finding}")
    assert read.status_code == 200 and "SECRET-FINDING-MARKER" in read.text
    later = datetime.now(timezone.utc) + timedelta(hours=1, minutes=1)
    services.access.clock = lambda: later
    assert pm.get(f"/api/objects/finding/{finding}").status_code == 403


def test_another_project_sees_nothing(site):
    app, services = site
    em = Person(app, "em")
    _, _, approval_id = _seed(em, services)
    outsider = Person(app, "outsider")
    assert outsider.session["projects"] == {"argo-cd": "em"}
    asked = outsider.post("/api/access-requests", {"object_type": "approval", "object_id": approval_id})
    assert asked.status_code == 404
    assert outsider.get(f"/api/approvals/{approval_id}").status_code == 404
    pm = Person(app, "pm")
    rid = pm.post("/api/access-requests", {"object_type": "finding",
                                           "object_id": f"{next(iter(services.tasks.tasks.values()))['run_id']}:1"}
                  ).json()["request"]["request_id"]
    assert outsider.post(f"/api/access-requests/{rid}/decision", {"decision": "deny"}).status_code == 404
    assert outsider.get("/api/access-requests", box="to_decide").json()["requests"] == []
