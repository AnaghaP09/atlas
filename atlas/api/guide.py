"""Serves the user guide: a list of pages and each page as Markdown text.

The pages live in ``docs/guide``. The web interface renders them with its own safe Markdown
renderer, so the server sends plain text only and never HTML.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse, PlainTextResponse

GUIDE_DIR = Path(__file__).resolve().parents[2] / "docs" / "guide"
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}$")

router = APIRouter()


def _title(path: Path) -> str:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                return line[2:].strip()
    except OSError:
        pass
    return path.stem.replace("-", " ").capitalize()


def pages(directory: Path = GUIDE_DIR) -> dict[str, str]:
    """Slug to title for every guide page on disk."""
    if not directory.is_dir():
        return {}
    return {p.stem: _title(p) for p in sorted(directory.glob("*.md")) if SLUG.match(p.stem)}


@router.get("/docs/guide/pages.json", include_in_schema=False)
def guide_index() -> JSONResponse:
    found = pages()
    return JSONResponse({"pages": sorted(found), "titles": found})


@router.get("/docs/guide/{name}", include_in_schema=False)
def guide_page(name: str) -> PlainTextResponse:
    slug = name[:-3] if name.endswith(".md") else name
    path = GUIDE_DIR / f"{slug}.md"
    if not SLUG.match(slug) or not path.is_file():
        return PlainTextResponse("Guide page not found.", status_code=404)
    return PlainTextResponse(path.read_text(encoding="utf-8"), media_type="text/markdown; charset=utf-8")
