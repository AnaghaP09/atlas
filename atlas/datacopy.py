"""On-demand "Data as of" copies: a project frozen as it stood when a release tag was made.

The server-side twin of the offline release-close builder in ``prep``. For one release tag it
builds a frozen copy as of the tag's date:

* the project's bug and flaky-test issues that were open then, each rebuilt from its own history
  (state, labels, title, body and comments as of the date; ``atlas.connectors.normalize``);
* the commits that lead up to the tag (the newest ones, without file lists);
* a component map of the tagged code (``atlas.components``) and commits per component in the 120
  days before the tag;
* a dependency scan of the tagged dependency list that counts only advisories published on or
  before the date;
* synthetic team capacity (marked synthetic), as in every release copy.

The same cleaning and leakage rules as the offline pipeline apply (``atlas.cleaning``): people get
pseudonyms, and the copy is refused if anything in it is dated after the tag. The copy is
registered with its project and a readable name, for example
"Example at release v2.0.0 · 3 Mar 2026 · 41 open issues".

API use is bounded: every read is counted against a fixed budget, and without a GitHub token only
small repositories are built (GitHub then allows 60 requests an hour; the issue history reads also
need a token for its GraphQL API, so the small-repository path uses REST reads and leaves out text
whose edit history cannot be read).
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from atlas.cleaning import People, check_export_people, check_temporal_leakage, collect_people, write_export
from atlas.components import build_component_map, owners_paths, source_paths
from atlas.connectors.dependency_scan import findings_as_of, gate_scan_records, query_advisories
from atlas.connectors.github import GitHubError, GitHubReader
from atlas.connectors.inventory import parse_inventory, pick_inventory_file
from atlas.connectors.normalize import commit_view, issue_as_of, to_record
from atlas.connectors.synthetic import capacity_records
from atlas.contracts import EgressPurpose, Provenance, Record, RunMode
from atlas.gateway import Gateway, GatewayRefused, RequestContext, SourceUnavailable
from atlas.graph import architecture_records
from atlas.store import AUTHORITATIVE_WRITERS, EvidenceDB, from_iso

MAX_REQUESTS = 1500          # with a token: hard ceiling on GitHub reads for one copy
MAX_ISSUES = 400             # with a token: issues read in full history
NO_TOKEN_REQUESTS = 45       # without a token GitHub allows 60 requests an hour; keep a margin
MAX_SOURCE_FILES = 800
COMMITS_KEPT = 30
CHURN_DAYS = 120
_TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+-]{0,99}$")
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


class CopyRefused(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class BudgetSpent(RuntimeError):
    pass


def readable_date(when: datetime) -> str:
    return f"{when.day} {_MONTHS[when.month - 1]} {when.year}"


def copy_id(project_id: str, tag: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", tag).strip("-.") or "tag"
    return f"{project_id}.release-close-{safe}"[:128]


def copy_title(name: str, tag: str, cutoff: datetime, open_issues: int) -> str:
    return f"{name} at release {tag} · {readable_date(cutoff)} · {open_issues} open issues"


@dataclass
class CopyJob:
    job_id: str
    project_id: str
    tag: str
    state: str = "queued"           # queued | running | done | failed | refused
    stage: str = "waiting"
    done: int = 0
    total: int = 0
    requests: int = 0
    budget: int = 0
    message: str = ""
    snapshot_id: str | None = None
    title: str | None = None
    started_at: str = ""
    finished_at: str | None = None
    counts: dict[str, Any] = field(default_factory=dict)

    def view(self) -> dict[str, Any]:
        return asdict(self)


class CountingReader(GitHubReader):
    """A GitHub reader that counts every request and stops at the budget."""

    def __init__(self, *args: Any, job: CopyJob, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.job = job

    def _spend(self) -> None:
        if self.job.requests >= self.job.budget:
            raise BudgetSpent(f"the request budget of {self.job.budget} GitHub reads is used up")
        self.job.requests += 1

    def graphql(self, document: str, variables: dict[str, Any]) -> dict[str, Any]:
        self._spend()
        return super().graphql(document, variables)

    def rest_conditional(self, path: str, params: dict[str, Any] | None = None):  # type: ignore[override]
        self._spend()
        return super().rest_conditional(path, params)


# ---------------------------------------------------------------- REST history (no token)


def rest_issue_raw(issue: Mapping[str, Any], timeline: list[Mapping[str, Any]], complete: bool,
                   cutoff: datetime) -> dict[str, Any]:
    """An issue in the shape ``issue_as_of`` reads, from REST data only.

    REST has no edit history. Text is kept only when it cannot have changed after the cut-off
    (nothing updated after it); otherwise the text is left out and listed as missing.
    """
    updated = from_iso(str(issue.get("updated_at") or issue.get("created_at")))
    events: list[dict[str, Any]] = []
    comments: list[dict[str, Any]] = []
    for e in timeline:
        kind = e.get("event")
        at = e.get("created_at")
        if kind == "commented":
            edited = e.get("updated_at") and from_iso(str(e["updated_at"])) > cutoff
            comments.append({"createdAt": at, "body": None if edited else e.get("body"), "databaseId": e.get("id"),
                             "author": {"login": (e.get("user") or e.get("actor") or {}).get("login")},
                             "userContentEdits": {"nodes": []}})
            continue
        if not at:
            continue
        if kind in ("labeled", "unlabeled"):
            events.append({"__typename": "LabeledEvent" if kind == "labeled" else "UnlabeledEvent", "createdAt": at,
                           "label": {"name": (e.get("label") or {}).get("name", "")}})
        elif kind == "closed":
            events.append({"__typename": "ClosedEvent", "createdAt": at, "stateReason": e.get("state_reason"), "closer": None})
        elif kind == "reopened":
            events.append({"__typename": "ReopenedEvent", "createdAt": at})
        elif kind == "renamed":
            r = e.get("rename") or {}
            events.append({"__typename": "RenamedTitleEvent", "createdAt": at, "previousTitle": r.get("from"),
                           "currentTitle": r.get("to")})
        elif kind == "cross-referenced":
            src = (e.get("source") or {}).get("issue") or {}
            events.append({"__typename": "CrossReferencedEvent", "createdAt": at, "willCloseTarget": False,
                           "source": {"__typename": "PullRequest" if src.get("pull_request") else "Issue",
                                      "number": src.get("number")}})
        elif kind == "referenced" and e.get("commit_id"):
            events.append({"__typename": "ReferencedEvent", "createdAt": at, "commit": {"oid": e.get("commit_id")}})
    return {
        "number": issue["number"], "title": issue.get("title") or "", "createdAt": issue.get("created_at"),
        "body": issue.get("body") if updated <= cutoff else None,
        "author": {"login": (issue.get("user") or {}).get("login")}, "url": issue.get("html_url"),
        "userContentEdits": {"nodes": []}, "edits_complete": updated <= cutoff,
        "timeline": events, "timeline_complete": complete, "comments_list": comments, "comments_complete": complete,
    }


# ---------------------------------------------------------------- the builder


class CopyBuilder:
    """Builds copies one at a time in a background thread and keeps their progress."""

    def __init__(self, *, data_dir: Path, gateway: Gateway, registry: Any, project: Callable[[str], Mapping[str, Any] | None],
                 authenticated: Callable[[], bool], clock: Callable[[], datetime] | None = None,
                 run_in_background: Callable[[Callable[[], None]], None] | None = None,
                 on_done: Callable[[CopyJob], None] | None = None) -> None:
        self.data_dir = Path(data_dir)
        self.gateway = gateway
        self.registry = registry
        self.project = project
        self.authenticated = authenticated
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.run_in_background = run_in_background or self._thread
        self.on_done = on_done
        self.jobs: dict[str, CopyJob] = {}
        self._busy = threading.Lock()

    @staticmethod
    def _thread(fn: Callable[[], None]) -> None:
        threading.Thread(target=fn, name="atlas-data-copy", daemon=True).start()

    # -------------------------------------------------------------- public

    def list(self, project_id: str) -> list[dict[str, Any]]:
        return [j.view() for j in sorted(self.jobs.values(), key=lambda j: j.started_at, reverse=True)
                if j.project_id == project_id]

    def get(self, job_id: str) -> CopyJob | None:
        return self.jobs.get(job_id)

    def start(self, project_id: str, tag: str) -> CopyJob:
        cfg = self.project(project_id)
        if cfg is None or not cfg.get("repository"):
            raise CopyRefused("this project has no repository to read")
        if not _TAG.match(tag or "") or ".." in tag:
            raise CopyRefused("name a release tag of this project")
        if any(j.state in ("queued", "running") for j in self.jobs.values()):
            raise CopyRefused("another copy is being built; wait for it to finish (one at a time keeps memory and API use low)")
        job = CopyJob(job_id=f"copy-{uuid.uuid4().hex[:10]}", project_id=project_id, tag=tag,
                      started_at=self.clock().isoformat(),
                      budget=MAX_REQUESTS if self.authenticated() else NO_TOKEN_REQUESTS)
        self.jobs[job.job_id] = job
        self.run_in_background(lambda: self._run(job, dict(cfg)))
        return job

    # -------------------------------------------------------------- the job

    def _run(self, job: CopyJob, cfg: Mapping[str, Any]) -> None:
        with self._busy:
            job.state = "running"
            try:
                self._build(job, cfg)
                job.state = "done"
            except CopyRefused as exc:
                job.state, job.message = "refused", exc.reason
            except BudgetSpent as exc:
                job.state, job.message = "failed", f"{exc}; nothing was registered"
            except (GatewayRefused, SourceUnavailable, GitHubError) as exc:
                job.state, job.message = "failed", f"a read failed: {exc}; nothing was registered"
            except Exception as exc:  # a failed copy is reported, never half-registered
                job.state, job.message = "failed", f"{type(exc).__name__}: {exc}; nothing was registered"
            finally:
                job.finished_at = self.clock().isoformat()
                if self.on_done is not None:
                    try:
                        self.on_done(job)
                    except Exception:
                        pass

    def _reader(self, job: CopyJob, repo: str, connector: str) -> CountingReader:
        owner, name = repo.split("/", 1)
        ctx = RequestContext(purpose=EgressPurpose.LIVE_INGEST, run_mode=RunMode.LIVE)
        return CountingReader(self.gateway, ctx, owner=owner, name=name, connector=connector, job=job)

    def _build(self, job: CopyJob, cfg: Mapping[str, Any]) -> None:
        repo = str(cfg["repository"])
        pid = job.project_id
        token = self.authenticated()
        git = self._reader(job, repo, "git")
        issues_reader = self._reader(job, repo, "issues")

        job.stage = "Reading the release tag"
        tag = git.tag_commit_time(job.tag)
        if not tag:
            raise CopyRefused(f"{job.tag} is not a tag of {repo}")
        cutoff = from_iso(str(tag["tagged_at"]))
        commit = str(tag["commit"])
        if cutoff > self.clock():
            raise CopyRefused("the tag is dated in the future")
        day = cutoff.strftime("%Y-%m-%d")
        labels = {k: v for k, v in dict(cfg.get("labels") or {}).items() if v and k in ("bug", "flaky", "critical", "regression")}
        label_filter = set(labels.values())

        job.stage = "Finding the issues open at the tag"
        if token:
            numbers = self._candidates_graphql(issues_reader, repo, day, sorted(label_filter))
        else:
            numbers, rest_issues = self._candidates_rest(issues_reader, cutoff, sorted(label_filter))
        if len(numbers) > (MAX_ISSUES if token else 20):
            limit = MAX_ISSUES if token else 20
            why = ("without a GitHub token GitHub allows 60 requests an hour, so only small repositories are built; "
                   "set GITHUB_TOKEN" if not token else "map a narrower bug or flaky-test label for this project")
            raise CopyRefused(f"{len(numbers)} issues would have to be read in full (the limit is {limit}); {why}")

        build_dir = self.data_dir / "local" / "build"
        build_dir.mkdir(parents=True, exist_ok=True)
        sid = copy_id(pid, job.tag)
        path = build_dir / f"{sid}.sqlite"
        if path.exists():
            path.unlink()
        db = EvidenceDB(path)
        counts: dict[str, Any] = {"issues": 0, "excluded_not_open": 0, "excluded_label": 0, "unknown_state": 0,
                                  "candidates": len(numbers)}
        try:
            job.total, job.done = len(numbers), 0
            job.stage = "Rebuilding each issue as of the tag"
            fetched = self.clock()
            for n in numbers:
                raw = issues_reader.issue(n) if token else self._rest_issue(issues_reader, rest_issues[n], cutoff)
                job.done += 1
                if not raw:
                    continue
                asof = issue_as_of(raw, cutoff)
                if asof is None:
                    continue
                if asof.view["state"] == "UNKNOWN":
                    counts["unknown_state"] += 1
                    continue
                if asof.view["state"] != "OPEN":
                    counts["excluded_not_open"] += 1
                    continue
                if label_filter and not (set(asof.view["labels"]) & label_filter):
                    counts["excluded_label"] += 1
                    continue
                record, extra = to_record("issues", asof, cutoff=cutoff, fetched_at=fetched, history_complete_through=fetched)
                db.put(record, AUTHORITATIVE_WRITERS["issues"], **extra)
                counts["issues"] += 1

            job.stage = "Reading the commits up to the tag"
            counts["commits"] = self._commits(git, db, commit, cutoff)

            job.stage = "Mapping components and imports"
            entries, complete = git.tree(commit)
            counts.update(self._components(git, db, commit, cutoff, token, entries, complete))

            job.stage = "Scanning dependencies as of the tag"
            inventory_file = str(cfg.get("security_inventory") or "") or pick_inventory_file([e["path"] for e in entries]) or ""
            counts.update(self._security(git, db, commit, cutoff, inventory_file, pid))

            for record, content, etype in capacity_records(cutoff):
                db.put(record, AUTHORITATIVE_WRITERS["capacity"], content=content, entity_type=etype, freshness_basis="per_run")
                counts["synthetic"] = counts.get("synthetic", 0) + 1

            job.stage = "Checking for anything after the tag"
            problems = check_temporal_leakage(db, cutoff)
            if problems:
                raise CopyRefused("the copy held data dated after the tag: " + "; ".join(problems[:3]))
            people = People(self.data_dir / "local", cfg.get("bot_accounts") or ())
            collect_people(db, people)

            job.stage = "Writing and registering the copy"
            name = str(cfg.get("name") or pid)
            title = copy_title(name, job.tag, cutoff, counts["issues"])
            manifest = {
                "project_id": pid, "repository": repo, "title": title,
                "release_or_case": f"release close {job.tag}", "release_tag": job.tag, "release_commit": commit,
                "cutoff": cutoff.isoformat(), "cutoff_rule": "the release tag time", "alias_variant": None,
                "counts": counts, "built_at": self.clock().isoformat(), "github_reads": job.requests,
                "notes": [
                    "Open bug and flaky-test issues as they stood at the release tag; capacity is synthetic.",
                    "Issues are found by their labels today; an issue whose label was removed later is not seen.",
                ] + ([] if token else ["Built without a GitHub token: text whose edit history cannot be read is left out."]),
                "data_modes": {"issues": "REAL", "architecture": "REAL (folders and import statements of the tagged code)",
                               "git": f"REAL (the {COMMITS_KEPT} newest commits up to the tag; commits per component in the "
                                      f"{CHURN_DAYS} days before it)",
                               "security": "REAL (rebuilt as of the tag; later advisories are left out)",
                               "capacity": "SYNTHETIC"},
            }
            target = self.data_dir / "local" / "snapshots" / "releases" / sid
            staging = build_dir / f"{sid}.export"  # outside the registry's folders until it is checked
            if staging.exists():
                shutil.rmtree(staging)
            write_export(db, staging, manifest, people, snapshot_id=sid)
            problems = check_export_people(staging, people)
            if problems:
                shutil.rmtree(staging, ignore_errors=True)
                raise CopyRefused("people's names survived cleaning: " + "; ".join(problems))
            people.write_handles_file()
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                self._replace_built(sid)
                shutil.rmtree(target)
            staging.rename(target)
        finally:
            db.close()
            try:
                path.unlink()
            except OSError:
                pass
        job.snapshot_id, job.title, job.counts = sid, title, counts
        job.stage = "Ready"
        job.message = f"{title}: {counts['issues']} open issues, {counts.get('components', 0)} components, {job.requests} GitHub reads"

    def _replace_built(self, sid: str) -> None:
        """A rebuilt copy replaces the old one: drop the old read-only file the registry made from it."""
        built = Path(self.registry.stores_dir) / "prepared" / f"{sid}.sqlite"
        with self.registry._lock:
            db = self.registry._open.pop(sid, None)
            if db is not None:
                db.close()
        for p in (built, built.with_suffix(".sha")):
            if p.exists():
                p.chmod(0o600)
                p.unlink()

    # -------------------------------------------------------------- parts

    @staticmethod
    def _candidates_graphql(reader: GitHubReader, repo: str, day: str, labels: list[str]) -> list[int]:
        numbers: set[int] = set()
        for label in labels or [""]:
            lab = f' label:"{label}"' if label else ""
            for q in (f"repo:{repo} is:issue is:open{lab} created:<={day}",
                      f"repo:{repo} is:issue is:closed{lab} created:<={day} closed:>={day}"):
                for node in reader.search_numbers(q, MAX_ISSUES + 1):
                    if node.get("number"):
                        numbers.add(int(node["number"]))
        return sorted(numbers)

    @staticmethod
    def _candidates_rest(reader: GitHubReader, cutoff: datetime, labels: list[str]) -> tuple[list[int], dict[int, Any]]:
        found: dict[int, Any] = {}
        for label in labels or [""]:
            params: dict[str, Any] = {"state": "all", "per_page": 100, "sort": "created", "direction": "desc"}
            if label:
                params["labels"] = label
            body = reader.rest("/issues", params) or []
            for issue in body:
                if not isinstance(issue, Mapping) or "pull_request" in issue:
                    continue
                created = from_iso(str(issue.get("created_at")))
                closed = from_iso(str(issue["closed_at"])) if issue.get("closed_at") else None
                if created <= cutoff and (closed is None or closed > cutoff):
                    found[int(issue["number"])] = issue
            if len(body) == 100:
                raise CopyRefused("without a GitHub token only small repositories are built: more than 100 issues "
                                  "carry this label, and GitHub allows 60 requests an hour; set GITHUB_TOKEN")
        return sorted(found), found

    @staticmethod
    def _rest_issue(reader: GitHubReader, issue: Mapping[str, Any], cutoff: datetime) -> dict[str, Any]:
        body = reader.rest(f"/issues/{issue['number']}/timeline", {"per_page": 100}) or []
        events = [e for e in body if isinstance(e, Mapping)]
        return rest_issue_raw(issue, events, len(events) < 100, cutoff)

    @staticmethod
    def _commits(reader: GitHubReader, db: EvidenceDB, commit: str, cutoff: datetime) -> int:
        body = reader.rest("/commits", {"sha": commit, "per_page": COMMITS_KEPT}) or []
        stored = 0
        for c in body if isinstance(body, list) else []:
            info = c.get("commit") or {}
            raw = {"sha": c.get("sha"), "message": info.get("message", ""),
                   "committed_at": (info.get("committer") or {}).get("date"),
                   "author": (c.get("author") or {}).get("login"), "parents": [p.get("sha") for p in c.get("parents", [])],
                   "files": []}
            asof = commit_view(raw, cutoff) if raw["sha"] and raw["committed_at"] else None
            if asof is None:
                continue
            record, extra = to_record("git", asof, cutoff=cutoff, fetched_at=cutoff, history_complete_through=cutoff)
            db.put(record, AUTHORITATIVE_WRITERS["git"], **extra)
            stored += 1
        return stored

    def _components(self, reader: GitHubReader, db: EvidenceDB, commit: str, cutoff: datetime, token: bool,
                    entries: list[dict[str, Any]], complete: bool) -> dict[str, Any]:
        if not entries:
            return {"components": 0, "component_note": "the file tree could not be read"}
        wanted, left_out = source_paths(entries, MAX_SOURCE_FILES if token else 0)
        texts: dict[str, str] = reader.source_files(commit, wanted) if wanted else {}
        owners: dict[str, str] = {}
        go_mod = None
        extra_files = owners_paths(entries) + (["go.mod"] if any(e.get("path") == "go.mod" for e in entries) else [])
        if extra_files:
            got = reader.source_files(commit, extra_files) if token else {
                p: t for p in extra_files if (t := (reader.file_text(p, commit) or (None,))[0]) is not None}
            go_mod = got.pop("go.mod", None)
            owners = got
        graph, summary = build_component_map(commit, entries, texts, go_mod_text=go_mod, owners_texts=owners)
        at = cutoff - timedelta(seconds=1)
        for record, content, etype in architecture_records(graph, at):
            content = dict(content, limitation=summary["note"]) if etype == "component" else content
            db.put(record, AUTHORITATIVE_WRITERS["architecture"], content=content, entity_type=etype, freshness_basis="identity")
        out = {"components": summary["components"], "component_edges": summary["edges"],
               "source_files_read": summary["files_read"], "source_files_left_out": left_out,
               "languages_read": summary["languages_read"], "tree_complete": complete}
        if token:
            out.update(self._churn(reader, db, commit, cutoff, graph.components))
        return out

    @staticmethod
    def _churn(reader: GitHubReader, db: EvidenceDB, commit: str, cutoff: datetime,
               components: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
        since = cutoff - timedelta(days=CHURN_DAYS)
        paths = [c for c in sorted(components) if c != "root" and re.fullmatch(r"[A-Za-z0-9._/-]+", c)]
        churn = reader.component_churn(commit, since.isoformat(), cutoff.isoformat(), paths) if paths else {}
        at = cutoff - timedelta(seconds=1)
        for component in paths:
            view = {"kind": "component_churn", "component": component, "commits": int(churn.get(component, 0)),
                    "window_days": CHURN_DAYS, "window_from": since.isoformat(), "window_to": cutoff.isoformat(),
                    "revision": commit, "owner_team": components[component].get("owner_team")}
            version = hashlib.sha256(json.dumps(view, sort_keys=True).encode()).hexdigest()[:16]
            source_id = f"churn/{component}"
            record = Record(
                record_id=f"git:{source_id}@{version}", source="git", source_id=source_id, revision=None,
                content_version_id=version, valid_from=at, observed_at=at, confirmed_through=cutoff,
                writer=AUTHORITATIVE_WRITERS["git"], provenance=Provenance.DERIVED, synthetic_ancestry=False,
                access_scopes=frozenset({"public"}), lineage=(), content_ref=f"db:git:{source_id}@{version}",
            )
            db.put(record, AUTHORITATIVE_WRITERS["git"], content=view, entity_type="component_churn", freshness_basis="identity")
        return {"churn_components": len(paths), "churn_commits_total": sum(int(v) for v in churn.values())}

    def _security(self, reader: GitHubReader, db: EvidenceDB, commit: str, cutoff: datetime, name: str,
                  pid: str) -> dict[str, Any]:
        if not name:
            return {"security": "no supported dependency list in the tagged code; no scan stored"}
        got = reader.file_text(name, commit)
        inventory = parse_inventory(name, got[0]) if got else None
        if inventory is None or not inventory.packages:
            return {"security": f"{name} is not readable at this tag; no scan stored"}
        ctx = RequestContext(purpose=EgressPurpose.LIVE_INGEST, run_mode=RunMode.LIVE)
        packages = list(inventory.packages)
        by_module, details, complete = query_advisories(self.gateway, ctx, packages, ecosystem=inventory.ecosystem)
        findings, excluded = findings_as_of(by_module, details, dict(packages), cutoff)
        records = gate_scan_records(scope_id=f"{pid}:{commit}", revision=commit, inventory=packages, complete=complete,
                                    findings=findings, at=cutoff, fetched_at=self.clock(), reconstructed=True,
                                    excluded_after_cutoff=excluded, scanner=f"OSV.dev query over {inventory.label}")
        for record, content, etype in records:
            db.put(record, AUTHORITATIVE_WRITERS["security"], content=content, entity_type=etype, freshness_basis="scan")
        return {"security_modules": len(packages), "security_findings": len(findings),
                "security_excluded_after_cutoff": excluded, "security_complete": complete,
                "security_list": inventory.file, "security_ecosystem": inventory.ecosystem}


def release_tags(gateway: Gateway, repo: str, pattern: str = "", limit: int = 20) -> list[dict[str, Any]]:
    """Release tags to pick from: published releases with their dates (newest first), matching the pattern."""
    owner, name = repo.split("/", 1)
    ctx = RequestContext(purpose=EgressPurpose.LIVE_INGEST, run_mode=RunMode.LIVE)
    reader = GitHubReader(gateway, ctx, owner=owner, name=name, connector="git")
    rx = re.compile(pattern) if pattern else None
    out = []
    for rel in reader.rest("/releases", {"per_page": 50}) or []:
        tag = str(rel.get("tag_name") or "")
        if not tag or rel.get("draft") or rel.get("prerelease") or (rx and not rx.search(tag)):
            continue
        out.append({"tag": tag, "date": rel.get("published_at") or rel.get("created_at"), "name": rel.get("name")})
    if not out:
        for t in reader.rest("/tags", {"per_page": 50}) or []:
            tag = str(t.get("name") or "")
            if tag and (not rx or rx.search(tag)):
                out.append({"tag": tag, "date": None, "name": tag})
    return out[:limit]
