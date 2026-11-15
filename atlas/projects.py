"""Projects: which source sets ATLAS watches, and what each project may export.

A project is a named set of read-only sources (repository, issue query, CI source, security
inventory, release-tag pattern). Each installation keeps its own list in a private file, by default
``<data dir>/local/projects.toml``, copied from ``deploy/projects.example.toml`` by setup.

Every run, snapshot, task, approval, notification and event carries the ``project_id`` it belongs to.
A person sees and changes only objects of a project they belong to, with the role they hold in that
project. Non-person principals need an explicit export grant per project:

* ``publish_targets``: the repositories this project's tasks may publish to;
* ``notification_audiences``: the webhook audiences that may receive this project's notifications.

When no projects file exists the registry is empty and permissive: any requested project id is
accepted (this keeps a fresh checkout and the test suite working without configuration).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class Project:
    project_id: str
    name: str = ""
    data_source_note: str = ""
    repository: str = ""
    ci: str = ""
    evaluation: bool = False
    labels: Mapping[str, str] = field(default_factory=dict)
    publish_targets: tuple[str, ...] = ()
    notification_audiences: tuple[str, ...] = ()


class ProjectRegistry:
    def __init__(self, projects: Mapping[str, Project] | None = None) -> None:
        self._projects: dict[str, Project] = dict(projects or {})

    @classmethod
    def load(cls, path: Path | str | None) -> "ProjectRegistry":
        if path is None:
            return cls()
        p = Path(path)
        if not p.is_file():
            return cls()
        data = tomllib.loads(p.read_text(encoding="utf-8"))
        return cls.from_mapping(data.get("projects") or {})

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ProjectRegistry":
        out: dict[str, Project] = {}
        for pid, entry in raw.items():
            entry = dict(entry or {})
            export = dict(entry.get("export") or {})
            out[str(pid)] = Project(
                project_id=str(pid),
                name=str(entry.get("name", pid)),
                data_source_note=str(entry.get("data_source_note", "")),
                repository=str(entry.get("repository", "")),
                ci=str(entry.get("ci", "")),
                evaluation=bool(entry.get("evaluation", False)),
                labels={str(k): str(v) for k, v in (entry.get("labels") or {}).items()},
                publish_targets=tuple(str(t) for t in export.get("publish_targets", [])),
                notification_audiences=tuple(str(a) for a in export.get("notification_audiences", [])),
            )
        return cls(out)

    def add(self, project: Project) -> None:
        """Add a project to the running registry (after it was written to the projects file)."""
        if project.project_id in self._projects:
            raise ValueError(f"project {project.project_id} already exists")
        self._projects = {**self._projects, project.project_id: project}

    # -- reads ---------------------------------------------------------------------------------

    @property
    def configured(self) -> bool:
        return bool(self._projects)

    def ids(self) -> list[str]:
        return sorted(self._projects)

    def get(self, project_id: str | None) -> Project | None:
        return self._projects.get(project_id or "")

    def exists(self, project_id: str | None) -> bool:
        """Known project, or any non-empty id when no projects are configured."""
        if not project_id:
            return False
        return project_id in self._projects if self.configured else True

    # -- export grants for non-person principals ------------------------------------------------

    def may_publish(self, project_id: str | None, target: str | None) -> bool:
        """A project may publish to ``target`` only when it is listed in its export grant.

        Without a projects file the installation-wide publication target is the only grant.
        """
        if not target:
            return False
        if not self.configured:
            return True
        project = self.get(project_id)
        return project is not None and target in project.publish_targets

    def may_notify(self, project_id: str | None, audience: str | None) -> bool:
        """A webhook audience receives a project's notifications only with an explicit grant.

        Notifications without a project (installation-wide events) follow the destination's own
        audience rule only.
        """
        if not project_id or not self.configured:
            return True
        project = self.get(project_id)
        return project is not None and (audience or "") in project.notification_audiences

    def describe(self, project_id: str) -> dict[str, Any]:
        project = self.get(project_id)
        if project is None:
            return {"project_id": project_id, "name": project_id, "data_source_note": "", "evaluation": False}
        return {"project_id": project.project_id, "name": project.name,
                "data_source_note": project.data_source_note, "repository": project.repository,
                "ci": project.ci, "evaluation": project.evaluation}


ANY_PROJECT = "*"
"""Project id of a demo-header caller in an installation with no projects configured and no default
project (a fresh checkout or a single-project development setup). Such a caller is not limited to
one project. Installations with a projects file, and every signed-in user, always act in one named
project."""


def default_projects_path(data_dir: Path | str) -> Path:
    return Path(data_dir) / "local" / "projects.toml"
