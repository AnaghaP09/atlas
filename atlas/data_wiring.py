"""Connect the data layer to the running server.

``connect_data`` builds the evidence side of ATLAS from configuration only:

* the snapshot registry (one live store per project, prepared snapshots, frozen snapshots);
* a run-bound evidence store for Scout and Sentinel;
* the snapshot port the API uses (ingest, freeze, list, graph, evidence, live evidence);
* the security reader and scanner for task security decisions;
* the forecast's open-items source;
* one poller per project that keeps the live dashboard up to date.

Nothing here names a project. Repositories, labels and CI types come from the projects file
(``ATLAS_PROJECTS_FILE`` or ``<data dir>/local/projects.toml``); the reference dataset for the
prepared snapshots comes from ``data/evaluator/reference.json``.
"""

from __future__ import annotations

import hashlib
import json
import threading
import tomllib
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from atlas.connectors.github import GitHubError, GitHubReader
from atlas.connectors.issues import IssuesConnector
from atlas.connectors.project import ProjectSources, SourceSet
from atlas.connectors.security import SecurityConnector, module_inventory_from_go_mod
from atlas.contracts import (
    EgressPurpose,
    Mode,
    OpenItem,
    Provenance,
    Record,
    RunContext,
    RunMode,
    SecurityContext,
)
from atlas.gateway import Gateway, GatewayRefused, RequestContext, SourceUnavailable, httpx_transport
from atlas.graph import ReleaseGraph
from atlas.snapshots import SnapshotError, SnapshotRegistry
from atlas.store import (
    AUTHORITATIVE_WRITERS,
    LIVE_SNAPSHOT_ID,
    SECURITY_PARTITION_READERS,
    ActionStore,
    EvidenceStore,
    from_iso,
    to_iso,
)

NOT_CONFIGURED = "no projects are configured: copy deploy/projects.example.toml to your projects file"
SECURITY_READER = next(iter(SECURITY_PARTITION_READERS))


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------------------------
# Project sources from the projects file
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ProjectConfig:
    """What the data layer needs to know about one project."""

    sources: ProjectSources
    ci: str = ""  # "prow", "github_actions" or ""
    labels: Mapping[str, str] = field(default_factory=dict)
    release_tag_pattern: str = ""
    security_inventory: str = ""


def load_project_configs(path: Path | None) -> dict[str, ProjectConfig]:
    """Read project sources from the projects file. Missing file: no projects."""
    if path is None or not Path(path).is_file():
        return {}
    raw = tomllib.loads(Path(path).read_text(encoding="utf-8")).get("projects") or {}
    out: dict[str, ProjectConfig] = {}
    for pid, entry in raw.items():
        entry = dict(entry or {})
        src = dict(entry.get("sources") or {})
        src.setdefault("repository", entry.get("repository") or None)
        if not src.get("repository"):
            src.pop("repository")
        out[str(pid)] = ProjectConfig(
            sources=ProjectSources.from_mapping(str(pid), src),
            ci=str(entry.get("ci", "")),
            labels={str(k): str(v) for k, v in (entry.get("labels") or {}).items()},
            release_tag_pattern=str(entry.get("release_tag_pattern", "")),
            security_inventory=str(entry.get("security_inventory", "")),
        )
    return out


def reference_repository(data_dir: Path) -> str | None:
    """Repository the committed prepared snapshots were built from (from the reference file)."""
    path = Path(data_dir) / "evaluator" / "reference.json"
    if not path.is_file():
        return None
    try:
        return (json.loads(path.read_text(encoding="utf-8")).get("project") or {}).get("repository")
    except ValueError:
        return None


# --------------------------------------------------------------------------------------------
# Run-bound evidence store for Scout and Sentinel
# --------------------------------------------------------------------------------------------


class RunEvidenceStore:
    """The ``Store`` contract over the snapshot registry. Every read is bound to the run's snapshot.

    Content is handed out only for records this store returned under a run context, so the
    resolver's ``content_of`` cannot read anything the run could not see.
    """

    data_mode = "REAL"

    def __init__(self, registry: SnapshotRegistry) -> None:
        self.registry = registry
        self._store = EvidenceStore(registry)
        self._content: dict[str, Any] = {}
        self._lock = threading.Lock()

    def _remember(self, ctx: RunContext, records: Sequence[Record]) -> list[Record]:
        db = self.registry.open(ctx)
        with self._lock:
            for r in records:
                if r.record_id not in self._content:
                    self._content[r.record_id] = db.content(r.record_id)
        return list(records)

    def put(self, record: Record, writer: str) -> None:
        raise PermissionError("runs read evidence; live writes go through the connectors")

    def get(self, ctx: RunContext, source: str, source_id: str) -> Record | None:
        rec = self._store.get(ctx, source, source_id)
        return self._remember(ctx, [rec])[0] if rec else None

    def get_record(self, ctx: RunContext, record_id: str) -> Record | None:
        rec = self._store.by_id(ctx, record_id)
        return self._remember(ctx, [rec])[0] if rec else None

    def read_content(self, ctx: RunContext, record: Record) -> str:
        if self._store.by_id(ctx, record.record_id) is None:
            raise PermissionError("record is not visible in this snapshot")
        content = self._store.content(ctx, record.record_id)
        return content if isinstance(content, str) else json.dumps(content, sort_keys=True, default=str)

    def history(self, ctx: RunContext, source: str, source_id: str) -> list[Record]:
        return self._remember(ctx, self._store.history(ctx, source, source_id))

    def search(self, ctx: RunContext, query: Mapping[str, Any]) -> list[Record]:
        return self._remember(ctx, self._store.search(ctx, query))

    def content(self, ctx: RunContext, record_id: str) -> Any:
        return self._store.content(ctx, record_id)

    def content_of(self, record: Record) -> str:
        """Content of a record already returned under a run context (empty string otherwise)."""
        with self._lock:
            value = self._content.get(record.record_id)
        if value is None:
            return ""
        return value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)


# --------------------------------------------------------------------------------------------
# Open items for the forecast
# --------------------------------------------------------------------------------------------


class SnapshotOpenItems:
    """Open bug and flaky-test issues visible in a run's snapshot at its cutoff."""

    def __init__(self, store: RunEvidenceStore, labels_for: Callable[[str], Mapping[str, str]]) -> None:
        self.store = store
        self.labels_for = labels_for

    def open_items_for(self, ctx: RunContext) -> list[OpenItem]:
        labels = self.labels_for(getattr(ctx, "project_id", "") or "")
        wanted = {v for k, v in labels.items() if v and k in ("flaky", "bug", "critical", "regression")}
        items: list[OpenItem] = []
        for rec in self.store.search(ctx, {"source": "issues", "limit": 100_000}):
            view = self.store.content(ctx, rec.record_id) or {}
            if not isinstance(view, Mapping) or view.get("kind", "issue") not in ("issue", None):
                continue
            if str(view.get("state", "")).lower() not in ("open", ""):
                continue
            issue_labels = frozenset(str(x) for x in view.get("labels", []))
            if wanted and not (issue_labels & wanted):
                continue
            created = _dt(view.get("created_at") or view.get("createdAt")) or rec.valid_from
            last = _dt(view.get("updated_at") or view.get("last_activity")) or rec.observed_at
            component = next((lab.split("/", 1)[1] for lab in sorted(issue_labels) if lab.startswith("sig/")), None)
            items.append(OpenItem(
                issue_id=str(view.get("number") or rec.source_id), title=str(view.get("title", "")),
                labels=issue_labels, created=created, last_activity=last, component=component,
                record_ids=(rec.record_id,), required_scopes=frozenset(rec.access_scopes) or frozenset({"public"}),
            ))
        return items


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return from_iso(str(value))
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------------------------
# Task security: reader over the action store and a scanner for the target repository
# --------------------------------------------------------------------------------------------


class ActionSecurityReader:
    """``SecurityReader`` over the action store: scan runs and findings as security records."""

    def __init__(self, action_store: ActionStore, component_of: Callable[[SecurityContext], str]) -> None:
        self.action_store = action_store
        self.component_of = component_of

    def read(self, ctx: SecurityContext) -> list[Record]:
        out: list[Record] = []
        root = self.component_of(ctx)
        for run in self.action_store.scan_runs(ctx, SECURITY_READER):
            ok = sorted(m for m, status in run["per_module"].items() if status == "ok")
            complete = run["completion"] == "complete"
            content = {
                "kind": "scan_run", "scan_run_id": run["scan_run_id"],
                "coverage": ok + ([root] if complete else []),
                "status": "success" if complete else run["completion"],
                "finding_ids": run["finding_ids"], "target_revision": run["revision"],
            }
            out.append(_security_record(f"scan_run/{run['scan_run_id']}", content, run["observed_at"]))
            for f in run.get("findings", []):
                fcontent = {"kind": "finding", "finding_id": f["finding_id"], "component": f.get("module", ""),
                            "severity": f.get("severity"), "fix_version": f.get("fixed_version")}
                out.append(_security_record(f"finding/{f['finding_id']}", fcontent, run["observed_at"]))
        return out


def _security_record(source_id: str, content: Mapping[str, Any], at: datetime) -> Record:
    text = json.dumps(content, sort_keys=True)
    version = hashlib.sha256(text.encode()).hexdigest()[:16]
    return Record(
        record_id=f"security:{source_id}@{version}", source="security", source_id=source_id, revision=None,
        content_version_id=version, valid_from=at, observed_at=at, confirmed_through=at,
        writer=AUTHORITATIVE_WRITERS["security"], provenance=Provenance.OBSERVED, synthetic_ancestry=False,
        access_scopes=frozenset({"security"}), lineage=(), content_ref=text, fetched_at=at,
    )


class TargetScanner:
    """Scans a task's destination repository at its base revision (dependencies from ``go.mod``).

    The inventory comes from the local checkout of the target repository; only the OSV queries
    leave the machine, through the gateway, under the task's security context.
    """

    def __init__(self, connector: SecurityConnector, target_dir: Path) -> None:
        self.connector = connector
        self.target_dir = Path(target_dir)

    def module_path(self) -> str:
        go_mod = self.target_dir / "go.mod"
        if go_mod.is_file():
            for line in go_mod.read_text(encoding="utf-8").splitlines():
                if line.startswith("module "):
                    return line.split(None, 1)[1].strip()
        return "target"

    def inventory(self) -> list[tuple[str, str]]:
        go_mod = self.target_dir / "go.mod"
        return module_inventory_from_go_mod(go_mod.read_text(encoding="utf-8")) if go_mod.is_file() else []

    def scan(self, ctx: SecurityContext) -> str:
        return self.connector.scan_for_task(ctx, self.inventory())

    def components(self, ctx: SecurityContext) -> list[str]:
        return [self.module_path()] + [m for m, _ in self.inventory()]


# --------------------------------------------------------------------------------------------
# Snapshot port for the API
# --------------------------------------------------------------------------------------------


class DataSnapshots:
    """The API's snapshot port over the real snapshot registry."""

    data_mode = "REAL"

    def __init__(self, registry: SnapshotRegistry, pollers: Mapping[str, "ProjectPoller"],
                 default_project: str | None, clock: Callable[[], datetime] = _now) -> None:
        self.registry = registry
        self.pollers = pollers
        self.default_project = default_project
        self.clock = clock

    def _project(self, project_id: str | None) -> str:
        return project_id or self.default_project or ""

    def ingest(self, sources: Sequence[str], project_id: str) -> Mapping[str, Any]:
        pid = self._project(project_id)
        poller = self.pollers.get(pid)
        if poller is None:
            raise SnapshotError(f"project {pid or '(none)'} has no configured sources")
        result = poller.poll_once()
        return {"project_id": pid, "snapshot_id": LIVE_SNAPSHOT_ID, **result}

    def freeze(self, project_id: str) -> Mapping[str, Any]:
        pid = self._project(project_id)
        info = self.registry.freeze(frozen_at=self.clock(), project_id=pid)
        return {"snapshot_id": info.snapshot_id, "project_id": pid, "release": None, "kind": "frozen",
                "cutoff": info.frozen_at, "alias_variant": None, "mode": "replay", "sha256": info.sha256,
                "data_mode": "REAL"}

    def list(self, release: str | None = None, project_id: str | None = None) -> list[Mapping[str, Any]]:
        out = []
        for snap in self.registry.list_snapshots(project_id):
            snap = dict(snap)
            snap["release"] = snap.get("release_or_case")
            if release is not None and release not in (snap.get("release"), snap.get("snapshot_id")):
                continue
            snap.setdefault("mode", "replay")
            snap["data_mode"] = "REAL"
            out.append(snap)
        return out

    def get(self, snapshot_id: str) -> Mapping[str, Any] | None:
        for snap in self.list():
            if snap["snapshot_id"] == snapshot_id:
                return snap
        return None

    def _ctx(self, snapshot_id: str) -> RunContext | None:
        snap = self.get(snapshot_id)
        if snap is None:
            return None
        cutoff = _dt(snap.get("cutoff") or snap.get("frozen_at")) or self.clock()
        return RunContext(run_id=f"read-{uuid.uuid4().hex[:8]}", snapshot_id=snapshot_id, mode=RunMode.REPLAY,
                          release_or_case=str(snap.get("release") or snapshot_id), cutoff=cutoff,
                          alias_variant=snap.get("alias_variant"), evidence_clock=cutoff,
                          project_id=snap.get("project_id") or "")

    def graph(self, snapshot_id: str) -> Mapping[str, Any]:
        ctx = self._ctx(snapshot_id)
        if ctx is None:
            return {"nodes": [], "edges": []}
        return ReleaseGraph(EvidenceStore(self.registry), ctx).to_json()

    def evidence(self, snapshot_id: str, record_id: str, content_version_id: str | None,
                 lineage: Sequence[str] = ()) -> Mapping[str, Any] | None:
        ctx = self._ctx(snapshot_id)
        if ctx is None:
            return None
        db = self.registry.open(ctx)
        rec = db.by_id(record_id)
        if rec is None or (content_version_id and rec.content_version_id != content_version_id):
            return None
        return {"record_id": rec.record_id, "source": rec.source, "source_id": rec.source_id,
                "content_version_id": rec.content_version_id, "valid_from": to_iso(rec.valid_from),
                "provenance": getattr(rec.provenance, "value", str(rec.provenance)),
                "synthetic_ancestry": rec.synthetic_ancestry, "access_scopes": sorted(rec.access_scopes),
                "project_id": ctx.project_id, "content": db.content(rec.record_id)}

    def live_evidence(self, project_id: str, record_id: str, content_version_id: str | None) -> Mapping[str, Any] | None:
        return self.registry.live_evidence(project_id, record_id, content_version_id)


# --------------------------------------------------------------------------------------------
# Per-project poller for the live dashboard
# --------------------------------------------------------------------------------------------


class ProjectPoller:
    """Polls one project's sources and keeps its live store and dashboard current.

    * Issues: one conditional GET of the recently updated issues; changed issues are enriched
      and stored through the issues connector (at most ``max_enrich`` per poll).
    * CI: GitHub Actions runs for projects with ``ci = "github_actions"``; other CI types are
      shown as not polled.
    * Releases: the latest release.
    A failed poll confirms nothing, so stored evidence ages normally.
    """

    def __init__(self, project_id: str, config: ProjectConfig, gateway: Gateway, registry: SnapshotRegistry,
                 feed: Any, *, clock: Callable[[], datetime] = _now, max_enrich: int = 5) -> None:
        self.project_id = project_id
        self.config = config
        self.gateway = gateway
        self.registry = registry
        self.feed = feed
        self.clock = clock
        self.max_enrich = max_enrich
        self.store = registry.store(project_id)
        ctx = RequestContext(purpose=EgressPurpose.LIVE_INGEST, run_mode=RunMode.LIVE)
        src = config.sources
        self.reader = GitHubReader(gateway, ctx, owner=src.owner, name=src.name) if src.repository else None
        self.actions = GitHubReader(gateway, ctx, owner=src.owner, name=src.name, connector="actions") if src.repository else None
        self.issues = IssuesConnector(self.store, self.reader, clock=clock)
        self.last_error: str | None = None
        self._lock = threading.Lock()

    def _labels(self) -> list[str]:
        return [v for k, v in self.config.labels.items() if v and k in ("flaky", "bug", "critical", "regression")]

    def poll_once(self) -> dict[str, Any]:
        with self._lock:
            if self.reader is None:
                return {"polled": False, "reason": "the project has no repository configured"}
            stored: list[dict[str, Any]] = []
            try:
                stored = self._poll_issues()
                ci = self._poll_ci()
                release = self._poll_release()
            except (GatewayRefused, SourceUnavailable, GitHubError) as exc:
                self.last_error = str(exc)
                return {"polled": False, "reason": f"poll failed: {exc}", "stored": len(stored)}
            self.last_error = None
            self.feed.record_poll(self.project_id, self.clock())
            for item in stored:
                self.feed.record_activity(self.project_id, item)
            self.feed.update_panels(self.project_id, {
                "open_issues": self._open_issue_panel(),
                "failing_ci": ci,
                "latest_release": [release] if release else [],
            })
            self.feed.connected = True
            return {"polled": True, "stored": len(stored), "ci_runs": len([r for r in ci if "run_id" in r])}

    def _poll_issues(self) -> list[dict[str, Any]]:
        labels = self._labels()
        params = {"state": "open", "sort": "updated", "direction": "desc", "per_page": 20}
        result = self.reader.rest_conditional("/issues", params)
        if result.validator_pending:
            self.reader.commit_validator(result.url)
        if result.unchanged and not result.pending_refresh:
            return []
        out: list[dict[str, Any]] = []
        live_ctx = RunContext(run_id="poll", snapshot_id=LIVE_SNAPSHOT_ID, mode=RunMode.LIVE, release_or_case="live",
                              cutoff=self.clock(), alias_variant=None, evidence_clock=self.clock(),
                              project_id=self.project_id)
        for issue in (result.body or []):
            if "pull_request" in issue:
                continue
            names = {lab.get("name") for lab in issue.get("labels", []) if isinstance(lab, Mapping)}
            if labels and not (names & set(labels)):
                continue
            current = self.store.current_live("issues", f"issue/{issue['number']}")
            if current is not None and (_dt(issue.get("updated_at")) or self.clock()) <= current.observed_at:
                continue
            if len(out) >= self.max_enrich:
                break
            for rec in self.issues.fetch_live({"number": issue["number"]}, live_ctx):
                out.append({"kind": "issue", "title": f"#{issue['number']} {issue.get('title', '')}",
                            "record_id": rec.record_id, "content_version_id": rec.content_version_id,
                            "source_revision": issue.get("updated_at"), "required_scopes": ["public"]})
        return out

    def _open_issue_panel(self) -> list[dict[str, Any]]:
        db = self.registry.live(self.project_id)
        rows = []
        for rec in db.search({"source": "issues", "limit": 200}):
            view = db.content(rec.record_id) or {}
            if str(view.get("state", "open")).lower() != "open":
                continue
            rows.append({"number": view.get("number"), "title": view.get("title"), "labels": view.get("labels", []),
                         "record_id": rec.record_id, "content_version_id": rec.content_version_id})
        return rows[-20:]

    def _poll_ci(self) -> list[dict[str, Any]]:
        if self.config.ci != "github_actions":
            return [{"note": f"CI runs for this project ({self.config.ci or 'not set'}) are not polled in this build"}]
        result = self.actions.rest_conditional("/actions/runs", {"per_page": 10})
        if result.validator_pending:
            self.actions.commit_validator(result.url)
        runs = (result.body or {}).get("workflow_runs", []) if isinstance(result.body, Mapping) else []
        out = []
        failing = [r for r in runs if r.get("conclusion") in ("failure", "timed_out", "startup_failure")]
        for run in runs[:10]:
            view = {"kind": "workflow_run", "run_id": run.get("id"), "name": run.get("name"),
                    "status": run.get("status"), "conclusion": run.get("conclusion"),
                    "branch": run.get("head_branch"), "head_sha": run.get("head_sha"),
                    "created_at": run.get("created_at"), "updated_at": run.get("updated_at"),
                    "html_url": run.get("html_url")}
            if not result.unchanged:
                self._store_run(view)
            if run in failing:
                out.append({k: view[k] for k in ("run_id", "name", "status", "conclusion", "branch", "updated_at")})
        if not out:
            out.append({"note": f"no failing runs among the last {min(len(runs), 10)} workflow runs"})
        return out

    def _store_run(self, view: Mapping[str, Any]) -> None:
        at = _dt(view.get("updated_at")) or self.clock()
        version = hashlib.sha256(json.dumps(view, sort_keys=True, default=str).encode()).hexdigest()[:16]
        source_id = f"workflow_run/{view['run_id']}"
        rec = Record(
            record_id=f"ci:{source_id}@{version}", source="ci", source_id=source_id, revision=None,
            content_version_id=version, valid_from=at, observed_at=at, confirmed_through=self.clock(),
            writer=AUTHORITATIVE_WRITERS["ci"], provenance=Provenance.OBSERVED, synthetic_ancestry=False,
            access_scopes=frozenset({"public"}), lineage=(), content_ref=f"db:ci:{source_id}@{version}",
            fetched_at=self.clock(),
        )
        self.store.put(rec, AUTHORITATIVE_WRITERS["ci"], content=dict(view), entity_type="workflow_run",
                       freshness_basis="fetch")

    def _poll_release(self) -> dict[str, Any] | None:
        result = self.reader.rest_conditional("/releases/latest")
        if result.validator_pending:
            self.reader.commit_validator(result.url)
        body = result.body if isinstance(result.body, Mapping) else None
        if not body:
            return None
        return {"tag": body.get("tag_name"), "published_at": body.get("published_at"), "name": body.get("name")}


class PollLoop:
    """Runs every project's poller on an interval in one background thread.

    Offline mode pauses polling with a reason; the interval is ``ATLAS_DASHBOARD_POLL_SECONDS``.
    """

    def __init__(self, pollers: Mapping[str, ProjectPoller], mode: Callable[[], Mode], interval_seconds: int = 60) -> None:
        self.pollers = dict(pollers)
        self.mode = mode
        self.interval = max(15, int(interval_seconds))
        self.paused_reason: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self) -> dict[str, Any]:
        if self.mode() is Mode.OFFLINE:
            self.paused_reason = "offline mode: polling of external sources is paused"
            return {"paused": self.paused_reason}
        self.paused_reason = None
        return {pid: p.poll_once() for pid, p in self.pollers.items()}

    def start(self) -> None:
        if self._thread is not None or not self.pollers:
            return

        def run() -> None:
            while not self._stop.is_set():
                try:
                    self.tick()
                except Exception:  # a poll error must never stop the server
                    pass
                self._stop.wait(self.interval)

        self._thread = threading.Thread(target=run, name="atlas-dashboard-poll", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


# --------------------------------------------------------------------------------------------
# connect_data
# --------------------------------------------------------------------------------------------


@dataclass
class DataLayer:
    registry: SnapshotRegistry
    store: RunEvidenceStore
    snapshots: DataSnapshots
    action_store: ActionStore
    security_reader: ActionSecurityReader
    scanner: TargetScanner
    open_items: SnapshotOpenItems
    gateway: Gateway
    projects: dict[str, ProjectConfig]
    pollers: dict[str, ProjectPoller]
    poll_loop: PollLoop


def projects_file(settings: Any, env: Mapping[str, str]) -> Path:
    explicit = env.get("ATLAS_PROJECTS_FILE")
    return Path(explicit) if explicit else Path(settings.data_dir) / "local" / "projects.toml"


def connect_data(settings: Any, env: Mapping[str, str], *, mode: Callable[[], Mode], feed: Any,
                 on_violation: Callable[[str, Mapping[str, Any]], None] | None = None,
                 transport: Any = None, target_dir: Path, clock: Callable[[], datetime] = _now,
                 default_project: str | None = None) -> DataLayer:
    data_dir = Path(settings.data_dir)
    projects = load_project_configs(projects_file(settings, env))
    sources = SourceSet(tuple(c.sources for c in projects.values()))
    registry = SnapshotRegistry(data_dir, sources=sources, reference_repository=reference_repository(data_dir))
    token = getattr(settings, "github_token", "")
    gateway = Gateway(transport or httpx_transport(), mode=mode, on_violation=on_violation,
                      tokens={"github": lambda: token.reveal() if hasattr(token, "reveal") else str(token or "")},
                      clock=clock,
                      sources=sources)
    store = RunEvidenceStore(registry)
    action_store = ActionStore(data_dir / "local" / "stores" / "actions.sqlite")
    security_connector = SecurityConnector(gateway, action_store=action_store, clock=clock)
    scanner = TargetScanner(security_connector, target_dir)
    reader = ActionSecurityReader(action_store, lambda ctx: scanner.module_path())
    open_items = SnapshotOpenItems(store, lambda pid: projects[pid].labels if pid in projects else {})
    pollers = {pid: ProjectPoller(pid, cfg, gateway, registry, feed, clock=clock)
               for pid, cfg in projects.items() if cfg.sources.repository}
    poll_loop = PollLoop(pollers, mode, int(env.get("ATLAS_DASHBOARD_POLL_SECONDS") or 60))
    snapshots = DataSnapshots(registry, pollers, default_project, clock)
    return DataLayer(registry, store, snapshots, action_store, reader, scanner, open_items, gateway, projects,
                     pollers, poll_loop)
