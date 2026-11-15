"""Add a project from a public repository link.

Three steps, each its own function:

1. ``detect``: read the repository's public facts through the permission gateway (GET only, the
   ``onboarding`` connector) and propose a project block. Every value says whether it was
   ``detected`` (with the evidence) or ``needs you`` (a person must fill or check it).
2. ``add_to_projects_file``: write a confirmed block to the projects file. The file is replaced in
   one step (write a temporary file, then rename), so a reader never sees half a file. The new
   project never gets a publish target or a notification audience: people add those by hand.
3. ``register_live``: make the running server know the project at once: the project list, the
   gateway's read-only rows for the repository, the dashboard poller and the project's automatic
   runs (the ones that spend model budget stay paused, as for every project).

Nothing here names a real project: everything comes from the repository that was given.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import quote

from atlas.connectors.github import GitHubError, GitHubReader
from atlas.connectors.inventory import INVENTORY_FILES, ecosystem_of
from atlas.contracts import EgressPurpose, RunMode
from atlas.gateway import Gateway, GatewayRefused, RequestContext, SourceUnavailable

DETECTED = "detected"
NEEDS_YOU = "needs you"
_REPO = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
_URL = re.compile(r"^(?:https?://)?(?:www\.)?github\.com/([^/\s]+)/([^/\s#?]+)")
PROJECT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
LABEL_KEYS = ("bug", "flaky", "critical", "regression")
CI_KINDS = ("github_actions", "prow", "")
_WRITE_LOCK = threading.Lock()


class OnboardingError(ValueError):
    """A request that cannot be done, with a reason a person can act on."""

    def __init__(self, reason: str, status: int = 400, error: str = "invalid_project") -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status
        self.error = error


def parse_repo(text: str) -> str:
    """``owner/name`` from a GitHub link or from ``owner/name`` itself."""
    raw = (text or "").strip()
    m = _URL.match(raw)
    repo = f"{m.group(1)}/{m.group(2)}" if m else raw.strip("/")
    repo = re.sub(r"\.git$", "", repo)
    if not _REPO.match(repo) or ".." in repo:
        raise OnboardingError("give a public GitHub repository link (https://github.com/owner/name) or owner/name")
    return repo


def project_id_for(repo: str) -> str:
    name = repo.split("/", 1)[1].lower()
    slug = re.sub(r"[^a-z0-9]+", "-", name).strip("-")[:40] or "project"
    return slug if slug[0].isalnum() else f"p-{slug}"[:40]


def display_name(repo: str) -> str:
    name = repo.split("/", 1)[1]
    words = re.split(r"[-_.]+", name)
    return " ".join(w[:1].upper() + w[1:] for w in words if w) or name


# ---------------------------------------------------------------- label rules


def _tokens(label: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", label.lower()) if t]


def _label_rank(key: str, label: str) -> tuple[int, str] | None:
    """How well a label name fits a meaning (lower is better), or None when it does not fit."""
    tokens = _tokens(label)
    if not tokens:
        return None
    if any(t in ("not", "non", "no", "possible", "maybe", "unconfirmed", "invalid") for t in tokens):
        return None
    if key == "bug":
        if tokens == ["bug"]:
            return 0, "the label is named bug"
        if tokens[-1] in ("bug", "bugs") and len(tokens) <= 3:
            return len(tokens), "the label name ends with bug"
        if tokens[0] == "bug" and len(tokens) == 2 and tokens[1] in ("report", "confirmed"):
            return 3, "the label name starts with bug"
        return None
    if key == "flaky":
        if any(t.startswith("flak") for t in tokens):
            return (0 if len(tokens) <= 2 else len(tokens)), "the label name mentions a flaky test"
        return None
    if key == "critical":
        if "critical" in tokens or "urgent" in tokens or tokens in (["p0"], ["priority", "p0"], ["blocker"]) \
                or tokens[-1] in ("p0", "blocker"):
            return len(tokens), "the label name says critical, urgent or blocker"
        return None
    if key == "regression":
        if "regression" in tokens:
            return len(tokens), "the label name mentions a regression"
        return None
    return None


def suggest_labels(labels: list[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """For each meaning, the best label and the evidence, or an empty value that needs a person."""
    out: dict[str, dict[str, Any]] = {}
    for key in LABEL_KEYS:
        scored = []
        for lab in labels:
            name = str(lab.get("name") or "")
            ranked = _label_rank(key, name)
            if ranked is not None:
                scored.append((ranked[0], name.lower(), name, ranked[1], str(lab.get("description") or "")))
        scored.sort()
        if scored:
            _, _, name, why, desc = scored[0]
            evidence = f"label '{name}': {why}" + (f" (description: {desc[:120]})" if desc else "")
            others = [s[2] for s in scored[1:4]]
            if others:
                evidence += f"; other matches: {', '.join(others)}"
            out[key] = {"value": name, "status": DETECTED, "evidence": evidence}
        else:
            out[key] = {"value": "", "status": NEEDS_YOU,
                        "evidence": f"no label among the repository's {len(labels)} labels matches the {key} rule"}
    return out


def tag_pattern(tags: list[str]) -> tuple[str, str]:
    """A release tag pattern for final releases, from the tags seen, with the evidence."""
    with_v = [t for t in tags if re.fullmatch(r"v\d+\.\d+\.\d+", t)]
    without_v = [t for t in tags if re.fullmatch(r"\d+\.\d+\.\d+", t)]
    if not with_v and not without_v:
        return "", f"none of the {len(tags)} tags read look like a version number (1.2.3 or v1.2.3)"
    if len(with_v) >= len(without_v):
        return r"^v[0-9]+\.[0-9]+\.[0-9]+$", f"{len(with_v)} of {len(tags)} tags look like v1.2.3"
    return r"^[0-9]+\.[0-9]+\.[0-9]+$", f"{len(without_v)} of {len(tags)} tags look like 1.2.3"


# ---------------------------------------------------------------- detection


@dataclass
class _Counter:
    n: int = 0


class _Reader:
    """GET reads of one repository's public facts through the gateway, counted."""

    def __init__(self, gateway: Gateway, repo: str) -> None:
        owner, name = repo.split("/", 1)
        ctx = RequestContext(purpose=EgressPurpose.LIVE_INGEST, run_mode=RunMode.LIVE)
        self.gh = GitHubReader(gateway, ctx, owner=owner, name=name, connector="onboarding")
        self.count = _Counter()

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        self.count.n += 1
        return self.gh.rest(path, params)


def detect(gateway: Gateway, repo_text: str, *, existing: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Read the repository's public facts and propose a project block. Makes about 10 GET requests."""
    repo = parse_repo(repo_text)
    existing = dict(existing or {})
    r = _Reader(gateway, repo)
    try:
        meta = r.get("")
        if not isinstance(meta, Mapping) or not meta.get("full_name"):
            raise OnboardingError(f"{repo} was not found, or it is not public", 404, "not_found")
        if meta.get("private"):
            raise OnboardingError(f"{repo} is private; only public repositories can be added", 400)
        repo = str(meta["full_name"])
        languages = r.get("/languages") or {}
        labels: list[Mapping[str, Any]] = []
        for page in (1, 2, 3):
            body = r.get("/labels", {"per_page": 100, "page": page}) or []
            labels.extend(x for x in body if isinstance(x, Mapping))
            if len(body) < 100:
                break
        releases = [x for x in (r.get("/releases", {"per_page": 30}) or []) if isinstance(x, Mapping)]
        tags = [x for x in (r.get("/tags", {"per_page": 100}) or []) if isinstance(x, Mapping)]
        root = r.get("/contents") or []
        root_files = {str(x.get("name")): str(x.get("type")) for x in root if isinstance(x, Mapping)}
        github_dir = r.get("/contents/.github") if root_files.get(".github") == "dir" else []
        github_files = {str(x.get("name")) for x in (github_dir or []) if isinstance(x, Mapping)}
        workflows = r.get("/actions/workflows", {"per_page": 30}) if "workflows" in github_files else {}
        go_mod = None
        if root_files.get("go.mod") == "file":
            got = r.gh.file_text("go.mod")
            r.count.n += 1
            go_mod = got[0] if got else None
    except (GatewayRefused, SourceUnavailable) as exc:
        raise OnboardingError(f"the repository could not be read: {exc}", 503, "not_connected") from exc
    except GitHubError as exc:
        if exc.auth_or_limit:
            raise OnboardingError("GitHub refused the read (rate limit or token); set GITHUB_TOKEN or try later",
                                  503, "not_connected") from exc
        raise OnboardingError(f"the repository could not be read: {exc}", 502, "source_error") from exc

    fields: list[dict[str, Any]] = []

    def add(key: str, label: str, value: Any, status: str, evidence: str) -> None:
        fields.append({"key": key, "label": label, "value": value, "status": status, "evidence": evidence})

    pid = project_id_for(repo)
    taken = pid in existing
    add("project_id", "Project id", f"{pid}-2" if taken else pid, NEEDS_YOU if taken else DETECTED,
        f"from the repository name{'; ' + pid + ' is already used' if taken else ''}")
    add("name", "Name", display_name(repo), DETECTED, "from the repository name; change it if the project has another name")
    add("repository", "Repository", repo, DETECTED, f"GitHub says the repository is {repo}"
        + (f"; {meta.get('description')}" if meta.get("description") else ""))
    add("default_branch", "Default branch", meta.get("default_branch") or "", DETECTED, "from the repository settings")

    # Releases: published releases first (they carry a date), then plain tags.
    final = [x for x in releases if not x.get("draft") and not x.get("prerelease") and x.get("tag_name")]
    tag_names = [str(t.get("name")) for t in tags if t.get("name")]
    pattern, why = tag_pattern([str(x["tag_name"]) for x in final] or tag_names)
    add("release_tag_pattern", "Release tags", pattern, DETECTED if pattern else NEEDS_YOU, why)
    release_rows = [{"tag": str(x["tag_name"]), "date": x.get("published_at") or x.get("created_at")} for x in final[:10]]
    if not release_rows and tag_names:
        for t in tags[:5]:
            sha = ((t.get("commit") or {}).get("sha")) or ""
            date = None
            if re.fullmatch(r"[0-9a-f]{40}", sha):
                c = r.get(f"/commits/{sha}") or {}
                date = ((c.get("commit") or {}).get("committer") or {}).get("date")
            release_rows.append({"tag": str(t.get("name")), "date": date})
    latest = release_rows[0] if release_rows else None
    add("latest_release", "Latest release", latest["tag"] if latest else "", DETECTED if latest else NEEDS_YOU,
        (f"{latest['tag']} on {str(latest['date'] or '')[:10]}; {len(final)} published releases, {len(tag_names)} tags read"
         if latest else "no published release or tag found"))

    # CI: GitHub Actions is read live; Prow needs its artefact location configured by hand.
    n_workflows = int((workflows or {}).get("total_count") or 0) if isinstance(workflows, Mapping) else 0
    names = [str(w.get("name")) for w in (workflows or {}).get("workflows", [])][:5] if isinstance(workflows, Mapping) else []
    prow = ".prow.yaml" in root_files or ".prow" in root_files
    if n_workflows:
        add("ci", "CI", "github_actions", DETECTED, f"{n_workflows} GitHub Actions workflows: {', '.join(names)}")
    elif prow:
        add("ci", "CI", "prow", NEEDS_YOU, "a Prow configuration file is present; Prow's artefact location is set by hand")
    else:
        add("ci", "CI", "", NEEDS_YOU, "no GitHub Actions workflows and no Prow configuration found")

    total = sum(int(v) for v in languages.values()) or 1
    top = sorted(languages.items(), key=lambda kv: -int(kv[1]))[:5]
    add("languages", "Languages", [k for k, _ in top], DETECTED,
        ", ".join(f"{k} {round(100 * int(v) / total)}%" for k, v in top) or "GitHub lists no languages")

    suggested = suggest_labels(labels)
    for key in ("bug", "flaky"):
        s = suggested[key]
        if not s["value"]:
            continue
        try:
            found = r.get("/issues", {"state": "open", "labels": quote(s["value"], safe=""), "per_page": 100}) or []
        except (GatewayRefused, SourceUnavailable, GitHubError):
            continue
        n = sum(1 for x in found if isinstance(x, Mapping) and "pull_request" not in x)
        more = "+" if len(found) >= 100 else ""
        if n == 0:
            s["status"] = NEEDS_YOU
            s["evidence"] += "; no open issue carries it now: clear it to read every open issue instead"
        else:
            s["evidence"] += f"; {n}{more} open issues carry it now"
    for key in LABEL_KEYS:
        s = suggested[key]
        add(f"labels.{key}", f"'{key}' label", s["value"], s["status"], s["evidence"])
    bug = suggested["bug"]["value"]
    add("issue_query", "Issue query", f'is:issue label:"{bug}"' if bug else "is:issue",
        DETECTED if bug else NEEDS_YOU, "issues with the bug label" if bug else "no bug label; all issues are read")

    owners = [n for n in ("OWNERS", "CODEOWNERS") if n in root_files] + \
        [f".github/{n}" for n in ("CODEOWNERS",) if n in github_files]
    add("owners", "Owner files", owners, DETECTED,
        f"found {', '.join(owners)}; teams in them name component owners" if owners
        else "no OWNERS or CODEOWNERS file at the top or in .github; components get no owner team")

    inventory = next((name for name in INVENTORY_FILES if root_files.get(name) == "file"), "")
    module = ""
    if go_mod:
        module = next((ln.split(None, 1)[1].strip() for ln in go_mod.splitlines() if ln.startswith("module ")), "")
    if inventory:
        add("security_inventory", "Dependency list", inventory, DETECTED,
            f"{inventory} is present: the dependency scan asks OSV.dev about its {ecosystem_of(inventory)} packages")
    else:
        add("security_inventory", "Dependency list", "", NEEDS_YOU,
            "no go.mod, package-lock.json, uv.lock, poetry.lock or requirements.txt at the top; no dependency scan")
    add("go_module", "Go module", module, DETECTED if module else DETECTED,
        f"module path from go.mod: {module}" if module else "no go.mod: import edges for Go are not read")
    add("publish_targets", "Publish targets", [], NEEDS_YOU,
        "never set here: add a test repository by hand in the projects file if this project may publish")

    size_kb = int(meta.get("size") or 0)
    facts = {"open_issues_and_pull_requests": int(meta.get("open_issues_count") or 0), "size_kb": size_kb,
             "archived": bool(meta.get("archived")), "has_issues": bool(meta.get("has_issues", True)),
             "releases": release_rows, "html_url": meta.get("html_url")}
    proposal = {"repository": repo, "fields": fields, "facts": facts, "api_requests": r.count.n}
    proposal["project"] = project_from_fields(fields)
    proposal["block"] = render_block({**proposal["project"], "project_id": proposal["project"]["id"]})
    return proposal


def project_from_fields(fields: list[Mapping[str, Any]]) -> dict[str, Any]:
    """The project entry from detection fields. Its id is under ``id``: responses are projected per
    project, and an object naming another ``project_id`` would be withheld from the caller."""
    v = {f["key"]: f["value"] for f in fields}
    project: dict[str, Any] = {
        "id": v.get("project_id", ""), "name": v.get("name", ""), "repository": v.get("repository", ""),
        "data_source_note": f"Release data: {v.get('name', '')} public project (github.com/{v.get('repository', '')})",
        "issue_query": v.get("issue_query", "is:issue"), "ci": v.get("ci", ""),
        "release_tag_pattern": v.get("release_tag_pattern", ""), "security_inventory": v.get("security_inventory", ""),
        "evaluation": False,
        "labels": {k: v.get(f"labels.{k}", "") for k in LABEL_KEYS},
        "sources": {k: x for k, x in (("go_module", v.get("go_module", "")),) if x},
    }
    return project


# ---------------------------------------------------------------- the block


def _q(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=True)


def render_block(project: Mapping[str, Any]) -> str:
    """TOML text for one project, in the projects file's own layout. Publish targets stay empty."""
    pid = str(project["project_id"])
    head = f"[projects.{_q(pid)}]"
    lines = [f"# Added from {project.get('repository')} with Add project. Publish targets are never set here.", head]
    for key in ("name", "data_source_note", "repository", "issue_query", "ci", "release_tag_pattern", "security_inventory"):
        lines.append(f"{key} = {_q(project.get(key, ''))}")
    lines.append(f"evaluation = {'true' if project.get('evaluation') else 'false'}")
    sources = dict(project.get("sources") or {})
    if sources:
        lines += ["", f"[projects.{_q(pid)}.sources]"]
        for key in sorted(sources):
            value = sources[key]
            if isinstance(value, list):
                lines.append(f"{key} = [{', '.join(_q(x) for x in value)}]")
            else:
                lines.append(f"{key} = {_q(value)}")
    lines += ["", f"[projects.{_q(pid)}.labels]"]
    for key, value in dict(project.get("labels") or {}).items():
        lines.append(f"{key} = {_q(value)}")
    lines += ["", f"[projects.{_q(pid)}.export]", "publish_targets = []", "notification_audiences = []"]
    return "\n".join(lines) + "\n"


def validate_project(body: Mapping[str, Any], existing: Mapping[str, Any]) -> dict[str, Any]:
    """A confirmed project entry, checked. Raises OnboardingError with the reason."""
    if not isinstance(body, Mapping):
        raise OnboardingError("send the project as an object")
    export = body.get("export") or {}
    if isinstance(export, Mapping) and (export.get("publish_targets") or export.get("notification_audiences")):
        raise OnboardingError("publish targets and notification audiences are added by hand in the projects file, "
                              "never through Add project")
    if body.get("publish_targets") or body.get("notification_audiences"):
        raise OnboardingError("publish targets and notification audiences are added by hand in the projects file, "
                              "never through Add project")
    pid = str(body.get("id") or body.get("project_id") or "")
    if not PROJECT_ID.match(pid):
        raise OnboardingError("the project id must be 1 to 40 lowercase letters, digits or hyphens")
    if pid in existing:
        raise OnboardingError(f"a project with the id {pid} already exists", 409, "conflict")
    repo = parse_repo(str(body.get("repository") or ""))
    for other_id, other in existing.items():
        other_repo = str(getattr(other, "repository", "") or (other.get("repository") if isinstance(other, Mapping) else ""))
        if other_repo.lower() == repo.lower():
            raise OnboardingError(f"{repo} is already watched as project {other_id}", 409, "conflict")
    ci = str(body.get("ci") or "")
    if ci not in CI_KINDS:
        raise OnboardingError("CI must be github_actions, prow or empty")
    pattern = str(body.get("release_tag_pattern") or "")
    if len(pattern) > 200:
        raise OnboardingError("the release tag pattern is too long")
    try:
        re.compile(pattern)
    except re.error as exc:
        raise OnboardingError(f"the release tag pattern is not a valid pattern: {exc}") from exc

    def text(key: str, limit: int = 200) -> str:
        value = str(body.get(key) or "")
        if len(value) > limit or "\n" in value or "\r" in value:
            raise OnboardingError(f"{key} must be one line of at most {limit} characters")
        return value

    labels_in = body.get("labels") or {}
    if not isinstance(labels_in, Mapping) or set(labels_in) - set(LABEL_KEYS):
        raise OnboardingError(f"labels may only name: {', '.join(LABEL_KEYS)}")
    labels = {}
    for key in LABEL_KEYS:
        value = str(labels_in.get(key) or "")
        if len(value) > 100 or "\n" in value:
            raise OnboardingError(f"the {key} label must be one line of at most 100 characters")
        labels[key] = value
    inventory = text("security_inventory", 100)
    if inventory and (ecosystem_of(inventory) is None or ".." in inventory or inventory.startswith("/")):
        raise OnboardingError("the dependency list must be go.mod, package-lock.json, uv.lock, poetry.lock or a requirements file")
    sources_in = body.get("sources") or {}
    if not isinstance(sources_in, Mapping) or set(sources_in) - {"go_module", "bot_accounts"}:
        raise OnboardingError("sources may only name go_module and bot_accounts")
    sources: dict[str, Any] = {}
    if sources_in.get("go_module"):
        sources["go_module"] = str(sources_in["go_module"])[:200]
    if sources_in.get("bot_accounts"):
        bots = [str(b)[:40] for b in sources_in["bot_accounts"]][:20]
        if any(not re.fullmatch(r"[A-Za-z0-9\[\]_-]+", b) for b in bots):
            raise OnboardingError("bot accounts must be account names")
        sources["bot_accounts"] = bots
    name = text("name", 80) or display_name(repo)
    return {
        "project_id": pid, "name": name, "repository": repo,
        "data_source_note": text("data_source_note", 200) or f"Release data: {name} public project (github.com/{repo})",
        "issue_query": text("issue_query", 200) or "is:issue", "ci": ci, "release_tag_pattern": pattern,
        "security_inventory": inventory, "evaluation": False, "labels": labels, "sources": sources,
    }


# ---------------------------------------------------------------- the projects file


def add_to_projects_file(path: Path, project: Mapping[str, Any]) -> str:
    """Append the project block and replace the file in one step. Returns the block written."""
    block = render_block(project)
    path = Path(path)
    with _WRITE_LOCK:
        current = path.read_text(encoding="utf-8") if path.is_file() else (
            "# Projects for ATLAS. A project is a set of read-only sources; publish targets are added by hand.\n")
        existing = tomllib.loads(current).get("projects") or {}
        if project["project_id"] in existing:
            raise OnboardingError(f"a project with the id {project['project_id']} already exists", 409, "conflict")
        new_text = current.rstrip("\n") + "\n\n" + block
        parsed = tomllib.loads(new_text).get("projects") or {}
        entry = parsed.get(project["project_id"]) or {}
        if entry.get("repository") != project["repository"] or (entry.get("export") or {}).get("publish_targets"):
            raise OnboardingError("the project block did not read back as written; nothing was changed", 500, "error")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".projects-", suffix=".toml", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(new_text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    return block


# ---------------------------------------------------------------- the running server


def register_live(services: Any, project: Mapping[str, Any], *, clock: Callable[[], datetime] | None = None) -> dict[str, Any]:
    """Make the running server watch the new project now. Returns what was started."""
    from atlas.projects import Project

    pid = str(project["project_id"])
    services.projects.add(Project(project_id=pid, name=str(project["name"]),
                                  data_source_note=str(project.get("data_source_note") or ""),
                                  repository=str(project["repository"]), ci=str(project.get("ci") or ""),
                                  evaluation=False, labels=dict(project.get("labels") or {})))
    wiring = getattr(services, "wiring", None)
    data = (getattr(wiring, "extra", {}) or {}).get("data") if wiring is not None else None
    if data is None:
        return {"polling": False, "reason": "the data layer is not connected on this server"}
    from atlas.connectors.project import ProjectSources, SourceSet
    from atlas.data_wiring import ProjectConfig, ProjectPoller
    from atlas.watchers import project_watchers

    src = {"repository": project["repository"], **dict(project.get("sources") or {})}
    cfg = ProjectConfig(sources=ProjectSources.from_mapping(pid, src), ci=str(project.get("ci") or ""),
                        labels=dict(project.get("labels") or {}),
                        release_tag_pattern=str(project.get("release_tag_pattern") or ""),
                        security_inventory=str(project.get("security_inventory") or ""))
    data.projects[pid] = cfg
    sources = SourceSet(tuple(c.sources for c in data.projects.values()))
    data.gateway.sources = sources  # read-only rows for the new repository; publication still refuses it
    data.registry.sources = sources
    token = data.gateway._tokens.get("github")
    authenticated = bool(token and token())
    clock = clock or getattr(wiring, "clock", None)
    kwargs = {"clock": clock} if clock else {}
    poller = ProjectPoller(pid, cfg, data.gateway, data.registry, services.feed, authenticated=authenticated,
                           history_path=Path(data.registry.stores_dir) / pid / "dashboard-history.json", **kwargs)
    data.pollers[pid] = poller
    data.poll_loop.pollers[pid] = poller
    added = project_watchers(services, pid, poller, float(data.poll_loop.interval or 60))
    for w in added:
        services.scheduler.add(w)
    services.run_in_background(poller.poll_once)  # the dashboard fills without waiting for the next tick
    return {"polling": True, "watchers": [w.watcher_id for w in added]}
