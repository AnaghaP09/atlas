"""Data sets are listed with plain names, a date and a test-copy flag; never only an id."""

from __future__ import annotations

from atlas.api.snapshot_labels import describe, is_test_copy, short_title
from tests.d.conftest import H


def test_release_and_case_titles_and_test_copies():
    rel = describe(None, {"snapshot_id": "release-close-v1.9.0", "kind": "releases", "release_or_case": "v1.9.0",
                          "cutoff": "2026-07-30T12:00:00+00:00"}, "KubeVirt")
    assert rel["title"] == "KubeVirt at release v1.9.0"
    assert rel["as_of_label"] == "30 Jul 2026" and rel["display_kind"] == "release" and not rel["is_test_copy"]
    case = describe(None, {"snapshot_id": "case-g01", "kind": "cases", "release_or_case": "G01"}, "KubeVirt")
    assert case["title"].startswith("Example case:")
    assert is_test_copy({"kind": "releases-aliased"}) and is_test_copy({"alias_variant": "aliased"})


def test_short_title_drops_tags_and_trims():
    assert short_title("[sig-network]  Passt   uses nearly 100% CPU") == "Passt uses nearly 100% CPU"
    assert len(short_title("x" * 200)) <= 60


def test_snapshot_list_carries_display_fields(client):
    body = client.get("/api/snapshots", headers=H("pm")).json()
    for s in body["snapshots"]:
        assert {"title", "display_kind", "is_test_copy", "as_of"} <= set(s)
        assert "snapshot" not in s["title"].lower()
