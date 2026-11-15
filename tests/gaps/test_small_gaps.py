"""Small gaps: the session answer without sign-in, and the publish dry run for older tasks."""

from __future__ import annotations

from types import SimpleNamespace

from atlas.contracts import TaskState
from atlas.publish_check import describe, stale_title

from tests.gaps.conftest import headers as h

LONG_TEXT = ("Demo target (sample code modelled on the real issue)\n\nSummary: Format IPv6 hosts correctly in "
             "netaddr.JoinHostPort\n\nChange to make in the demo target:\nWrap IPv6 hosts in square brackets.")


def _task(title: str, message: str):
    return SimpleNamespace(task_id="task-1", state=TaskState.APPROVED_B, destination_repo="example/target",
                           base_ref="main", publication_ref="atlas/act-1", task_text=LONG_TEXT,
                           artefact={"title": title, "commit_message": message, "body": "body",
                                     "changed_files": {"netaddr/netaddr.go": "package netaddr\n"}})


def test_session_answers_that_sign_in_is_off_instead_of_404(server):
    services, client, _ = server
    out = client.get("/api/session", headers={"Accept": "application/json"})
    assert out.status_code == 200 and out.json() == {"sign_in": False, "identity": "header"}
    assert "/api/session" not in client.get("/api/openapi.json").json()["paths"]  # not a documented route
    assert client.get("/api/no-such-route", headers=h("pm")).status_code == 404


def test_dry_run_shows_a_short_title_as_it_is():
    view = describe(_task("Draft: Format IPv6 hosts correctly in netaddr.JoinHostPort",
                          "Fix: Format IPv6 hosts correctly in netaddr.JoinHostPort"))
    assert "must_be_prepared_again" not in view
    assert view["pull_request"]["title"] == "Draft: Format IPv6 hosts correctly in netaddr.JoinHostPort"
    assert view["network_requests_made"] == 0


def test_dry_run_says_an_older_task_must_be_prepared_again():
    old = _task("Draft: " + LONG_TEXT, "Fix: " + LONG_TEXT)
    assert stale_title(old.artefact)
    view = describe(old)
    assert view["must_be_prepared_again"] is True and "Prepare the task again" in view["reason"]
    assert view["title_after_preparing_again"] == "Draft: Format IPv6 hosts correctly in netaddr.JoinHostPort"
    assert view["commit_message_after_preparing_again"] == "Fix: Format IPv6 hosts correctly in netaddr.JoinHostPort"
    assert view["pull_request"]["title"].startswith("Draft: Demo target")  # the frozen title is shown unchanged
