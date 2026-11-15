"""Where a project's evidence comes from, read from the loaded project configuration.

Nothing here names a real project: every value comes from the configuration (for example the
``sources`` section of a project in ``deploy/projects.yaml``). With no configuration, no
upstream source is allowed and every connector request is refused.

Expected keys (all optional unless a connector needs them)::

    repository: owner/name                 # issue tracker and git host repository
    go_module: example.org/project         # module path for import-derived dependency edges
    ci:
      artifact_base: https://storage.example/bucket   # job artefacts: <base>/<job path>/build-log.txt
      view_hosts: [ci.example.org]                     # hosts of job-view links found in issues
      view_prefix: /view/gs/bucket                     # path prefix of job-view links
      listing_prefixes: [logs/periodic-project-]       # job folders to list for the live CI panel
    bot_accounts: [project-bot]            # automation accounts that are not people
    source_url: https://forge.example/owner/name.git   # read-only clone URL for preparation
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ProjectSources:
    project_id: str
    repository: str | None = None
    go_module: str | None = None
    ci_artifact_base: str | None = None
    ci_view_hosts: tuple[str, ...] = ()
    ci_view_prefix: str | None = None
    ci_listing_prefixes: tuple[str, ...] = ()
    bot_accounts: tuple[str, ...] = ()
    source_url: str | None = None

    @property
    def owner(self) -> str | None:
        return self.repository.split("/", 1)[0] if self.repository else None

    @property
    def name(self) -> str | None:
        return self.repository.split("/", 1)[1] if self.repository else None

    @property
    def ci_artifact_host(self) -> str | None:
        return urlsplit(self.ci_artifact_base).hostname if self.ci_artifact_base else None

    @property
    def ci_artifact_prefix(self) -> str | None:
        return urlsplit(self.ci_artifact_base).path.rstrip("/") if self.ci_artifact_base else None

    @classmethod
    def from_mapping(cls, project_id: str, data: Mapping[str, Any] | None) -> "ProjectSources":
        data = dict(data or {})
        ci = dict(data.get("ci") or {})
        repo = data.get("repository")
        if repo is not None and (not isinstance(repo, str) or repo.count("/") != 1):
            raise ValueError(f"project {project_id}: repository must be 'owner/name'")
        base = ci.get("artifact_base")
        if base is not None and urlsplit(base).scheme != "https":
            raise ValueError(f"project {project_id}: ci.artifact_base must be an https URL")
        return cls(
            project_id=project_id,
            repository=repo,
            go_module=data.get("go_module"),
            ci_artifact_base=base.rstrip("/") if base else None,
            ci_view_hosts=tuple(ci.get("view_hosts") or ()),
            ci_view_prefix=ci.get("view_prefix"),
            ci_listing_prefixes=tuple(str(x).lstrip("/") for x in (ci.get("listing_prefixes") or ())),
            bot_accounts=tuple(data.get("bot_accounts") or ()),
            source_url=data.get("source_url"),
        )


@dataclass(frozen=True)
class SourceSet:
    """All configured projects. Empty by default: nothing is allowed until configured."""

    projects: tuple[ProjectSources, ...] = field(default_factory=tuple)

    def repositories(self) -> set[str]:
        return {p.repository.lower() for p in self.projects if p.repository}

    def by_repository(self, repo: str) -> ProjectSources | None:
        for p in self.projects:
            if p.repository and p.repository.lower() == repo.lower():
                return p
        return None

    def ci_artifact_rules(self) -> set[tuple[str, str]]:
        return {(p.ci_artifact_host, p.ci_artifact_prefix) for p in self.projects if p.ci_artifact_base}

    def ci_listing_rules(self) -> set[tuple[str, str, tuple[str, ...]]]:
        """(host, bucket path, allowed listing prefixes) for projects that list their CI job folders."""
        return {(p.ci_artifact_host, p.ci_artifact_prefix, p.ci_listing_prefixes)
                for p in self.projects if p.ci_artifact_base and p.ci_listing_prefixes}

    def ci_view_rules(self) -> set[tuple[str, str]]:
        out = set()
        for p in self.projects:
            for host in p.ci_view_hosts:
                out.add((host, p.ci_view_prefix or "/"))
        return out

    @classmethod
    def from_projects(cls, projects: Mapping[str, Mapping[str, Any]] | Sequence[ProjectSources]) -> "SourceSet":
        if isinstance(projects, Mapping):
            return cls(tuple(ProjectSources.from_mapping(pid, (cfg or {}).get("sources", cfg)) for pid, cfg in projects.items()))
        return cls(tuple(projects))


def data_dir() -> Path:
    """The data directory: ``ATLAS_DATA_DIR`` or ``data`` relative to the working directory."""
    return Path(os.environ.get("ATLAS_DATA_DIR") or "data")
