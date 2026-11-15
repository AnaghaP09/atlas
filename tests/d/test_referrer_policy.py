"""Browsers send `Origin: null` for a form post under a no-referrer policy, which the site guard
refuses. Pages must use same-origin so the sign-in form keeps its real Origin."""

from pathlib import Path


def test_pages_use_same_origin_referrer_policy(client):
    for path in ["/", "/api/health"]:
        response = client.get(path)
        assert response.headers.get("referrer-policy") == "same-origin", path


def test_ui_page_meta_matches():
    html = Path(__file__).resolve().parents[2].joinpath("ui", "index.html").read_text(encoding="utf-8")
    assert '<meta name="referrer" content="same-origin" />' in html
    assert 'content="no-referrer"' not in html
