"""Runs are described in plain words, not by their id."""

from types import SimpleNamespace

from atlas.api.routes_runs import _run_label


def adm(trigger):
    return SimpleNamespace(trigger=trigger, admitted_at="2026-11-14T10:00:00+00:00")


SERVICES = SimpleNamespace(projects=None, snapshots=None)


def test_investigation_names_the_issue():
    out = _run_label(SERVICES, adm({"trigger": "failure", "event_id": "18234"}), None)
    assert out["title"] == "Investigation · issue #18234"
    assert out["started_at"] == "2026-11-14T10:00:00+00:00"


def test_question_is_shortened():
    out = _run_label(SERVICES, adm({"trigger": "question", "question": "x " * 80}), None)
    assert out["kind_label"] == "Question"
    assert len(out["subject"]) <= 70 and out["subject"].endswith("…")


def test_release_close_falls_back_to_the_release():
    out = _run_label(SERVICES, adm({"trigger": "release_close", "release_id": "v1.9.0"}), None)
    assert out["title"] == "Release close · release v1.9.0"


def test_unknown_kind_is_still_readable():
    assert _run_label(SERVICES, adm({"trigger": "some_new_kind"}), None)["title"] == "Some new kind"
