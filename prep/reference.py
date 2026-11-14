"""The reference dataset definition (``data/evaluator/reference.json``) and preparation paths.

Preparation code names no project: repository, module, CI locations, golden cases and release
windows all come from the definition file. Paths derive from ``ATLAS_DATA_DIR`` (default
``data``), built with pathlib.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from atlas.connectors.project import ProjectSources, SourceSet, data_dir


@dataclass(frozen=True)
class Reference:
    project: ProjectSources
    raw: dict[str, Any]

    @property
    def cases(self) -> dict[str, dict[str, Any]]:
        return self.raw["golden_cases"]

    @property
    def windows(self) -> dict[str, Any]:
        return self.raw["windows"]

    @property
    def releases(self) -> dict[str, Any]:
        return self.raw["releases"]

    @property
    def alias_words(self) -> list[tuple[str, str]]:
        return [tuple(pair) for pair in self.raw["project"].get("alias_words", [])]

    @property
    def job_pattern(self) -> re.Pattern[str] | None:
        pattern = self.raw["project"].get("job_name_pattern")
        return re.compile(r"\b" + pattern) if pattern else None

    @property
    def ticket_prefixes(self) -> tuple[str, ...]:
        return tuple(self.raw["project"].get("ticket_prefixes", []))

    @property
    def sources(self) -> SourceSet:
        return SourceSet((self.project,))

    def prohibited_prs(self, case_id: str) -> set[int]:
        return set(self.cases.get(case_id, {}).get("prohibited_prs", []))

    def notes(self, case_id: str) -> list[str]:
        return list(self.cases.get(case_id, {}).get("notes", []))

    def view_link_pattern(self) -> re.Pattern[str]:
        """Job-view links found in issue text, capturing the job path below the view prefix."""
        hosts = "|".join(re.escape(h) for h in self.project.ci_view_hosts) or r"(?!)"
        prefix = re.escape((self.project.ci_view_prefix or "").strip("/"))
        return re.compile(r"https?://(?:" + hosts + r")/+" + prefix + r"/([A-Za-z0-9._~/-]+?)(?=[)\]\s>\"'`]|$)")


def paths() -> dict[str, Path]:
    base = data_dir()
    local = base / "local"
    return {
        "data": base,
        "local": local,
        "raw": local / "raw",
        "export": local / "export",
        "build": local / "build",
        "src": local / "src",
        "reference": base / "evaluator" / "reference.json",
    }


@lru_cache(maxsize=4)
def _load(path: str) -> Reference:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    project = ProjectSources.from_mapping(raw["project"].get("project_id", "reference"), raw["project"])
    return Reference(project, raw)


def reference(path: Path | None = None) -> Reference:
    return _load(str(path or paths()["reference"]))
