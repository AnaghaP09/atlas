"""The user guide is served as a page list and plain Markdown, never as HTML."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from atlas.api import guide


def client() -> TestClient:
    app = FastAPI()
    app.include_router(guide.router)
    return TestClient(app)


def test_index_lists_pages_with_titles():
    body = client().get("/docs/guide/pages.json").json()
    assert "glossary" in body["pages"]
    assert body["titles"]["glossary"]


def test_page_is_markdown_text():
    r = client().get("/docs/guide/glossary.md")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert r.text.lstrip().startswith("#")


def test_unknown_or_unsafe_names_are_not_found():
    c = client()
    assert c.get("/docs/guide/nope.md").status_code == 404
    assert c.get("/docs/guide/..%2f..%2fREADME.md").status_code == 404
    assert c.get("/docs/guide/UPPER.md").status_code == 404


def test_every_page_on_disk_is_listed():
    on_disk = {p.stem for p in Path(guide.GUIDE_DIR).glob("*.md")}
    assert set(guide.pages()) == on_disk
