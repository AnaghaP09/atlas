"""Maps a delegated task onto the demo target repository.

The demo target holds sample code, so a task found in a real project cannot be applied to it
directly. The target manifest lists which kinds of task the sample code can answer and the exact
change to make there. A task that matches no entry is refused.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

CHANGE_HEADING = "Change to make in the demo target"
CAUSE_HEADING = "Cause verified in the real project (background only)"
SUMMARY_PREFIX = "Summary: "


class NoTargetMapping(ValueError):
    pass


@dataclass(frozen=True)
class MappedTask:
    mapping_id: str
    title: str
    label: str
    data_mode: str
    change: str
    cause: str
    scope: tuple[str, ...]
    behaviour_check: str

    @property
    def task_text(self) -> str:
        parts = [self.label, f"{SUMMARY_PREFIX}{self.title}", f"{CHANGE_HEADING}:{chr(10)}{self.change}",
                 f"{CAUSE_HEADING}:{chr(10)}{self.cause}"]
        return (chr(10) * 2).join(parts)


@dataclass(frozen=True)
class TargetManifest:
    label: str
    data_mode: str
    mappings: tuple[dict, ...]

    @classmethod
    def load(cls, path: Path) -> "TargetManifest | None":
        if not path.is_file():
            return None
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        return cls(str(data.get("label", "Demo target")), str(data.get("data_mode", "SYNTHETIC")),
                   tuple(data.get("mapping", [])))

    def map(self, classification: str, cause_texts: Sequence[str]) -> MappedTask:
        """The mapped task for an investigation, or NoTargetMapping when the sample code cannot answer it."""
        haystack = " ".join(cause_texts).lower()
        for m in self.mappings:
            if classification.upper() not in [c.upper() for c in m.get("classifications", [])]:
                continue
            if not all(k.lower() in haystack for k in m.get("keywords", [])):
                continue
            return MappedTask(str(m["id"]), str(m.get("title") or m["id"]), self.label, self.data_mode, str(m["change"]),
                              next((t for t in cause_texts if t), "not stated"),
                              tuple(m.get("scope", [])), str(m.get("behaviour_check", "")))
        raise NoTargetMapping("the demo target has no sample code for this task")


def change_section(task_text: str) -> str:
    """The part of a mapped task text that the diff is judged against (the whole text if unmapped)."""
    if CHANGE_HEADING not in task_text:
        return task_text
    after = task_text.split(CHANGE_HEADING + ":", 1)[1]
    return after.split(CAUSE_HEADING, 1)[0].strip()


def summary_line(task_text: str, limit: int = 72) -> str:
    """One short line for a commit message or pull request title."""
    for line in task_text.splitlines():
        if line.startswith(SUMMARY_PREFIX):
            return line[len(SUMMARY_PREFIX):].strip()[:limit]
    first = next((line.strip() for line in task_text.splitlines() if line.strip()), "Proposed change")
    return first if len(first) <= limit else first[: limit - 3].rstrip() + "..."
