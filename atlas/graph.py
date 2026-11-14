"""Release knowledge graph and import-derived dependency edges.

Dependency edges come from static parsing of Go ``import`` declarations at a pinned revision; no
repository code is built or run. Imports are lifted from package to component. Runtime
relationships (RPC calls, Kubernetes API calls, roll-outs) cannot be derived this way and are
reported as ``unknown``, never inferred. Imports in ``_test.go`` files give ``test_depends_on``
edges, not ``depends_on``.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from atlas.contracts import Provenance, Record, RunContext
from atlas.store import AUTHORITATIVE_WRITERS, EvidenceStore

MODULE = "kubevirt.io/kubevirt"
RUNTIME_LIMITATION = (
    "Dependency edges are derived from Go import statements only. Runtime relationships such as "
    "RPC calls, Kubernetes API interactions and roll-outs are not visible this way and are unknown."
)

_IMPORT_SINGLE = re.compile(r'^\s*import\s+(?:[A-Za-z_.][A-Za-z0-9_]*\s+)?"([^"]+)"', re.M)
_IMPORT_BLOCK = re.compile(r"^\s*import\s*\((.*?)\)", re.S | re.M)
_IMPORT_LINE = re.compile(r'^\s*(?:[A-Za-z_.][A-Za-z0-9_]*\s+)?"([^"]+)"', re.M)
_BUILD_TAG = re.compile(r"^//go:build\s+(.+)$", re.M)


def _strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?m)//.*$", "", src)


def parse_imports(source: str) -> list[str]:
    """Import paths declared in one Go file (static parsing only)."""
    header = source.split("\nfunc ", 1)[0]
    build = _BUILD_TAG.search(header)
    del build  # build constraints do not change which packages are imported
    text = _strip_comments(header)
    imports: list[str] = []
    for block in _IMPORT_BLOCK.findall(text):
        imports.extend(_IMPORT_LINE.findall(block))
    imports.extend(_IMPORT_SINGLE.findall(text))
    seen: list[str] = []
    for imp in imports:
        if imp not in seen:
            seen.append(imp)
    return seen


def component_of(path: str) -> str:
    """Map a repository path (file or package directory) to a component name."""
    parts = PurePosixPath(path).parts
    if not parts:
        return "root"
    if parts[0] in ("pkg", "cmd", "staging") and len(parts) >= 2:
        if len(parts) == 2 and parts[1].endswith(".go"):
            return parts[0]
        return f"{parts[0]}/{parts[1]}"
    if parts[0] == "tests":
        if len(parts) >= 3:
            return f"tests/{parts[1]}"
        return "tests"
    return parts[0]


def component_of_import(imp: str, module: str = MODULE) -> str | None:
    if not imp.startswith(module + "/"):
        return None
    return component_of(imp[len(module) + 1:] + "/x.go")


def _owner_team(owners_text: str) -> str | None:
    """Owner team alias from an OWNERS file's labels (never personal handles)."""
    labels = re.findall(r"^\s*-\s*(sig/[A-Za-z0-9-]+|area/[A-Za-z0-9-]+)\s*$", owners_text, re.M)
    return labels[0] if labels else None


@dataclass
class ArchitectureGraph:
    revision: str
    components: dict[str, dict[str, Any]] = field(default_factory=dict)
    edges: dict[tuple[str, str, str], int] = field(default_factory=dict)

    def add_edge(self, src: str, dst: str, kind: str) -> None:
        if src == dst:
            return
        key = (src, dst, kind)
        self.edges[key] = self.edges.get(key, 0) + 1

    def depends_on(self, component: str) -> set[str]:
        return {d for (s, d, k) in self.edges if s == component and k == "depends_on"}

    def dependents(self, component: str) -> set[str]:
        return {s for (s, d, k) in self.edges if d == component and k == "depends_on"}

    def one_hop(self, component: str) -> set[str]:
        """Components one depends_on edge away in either direction."""
        return self.depends_on(component) | self.dependents(component)

    def fan_in(self, component: str) -> int:
        return len(self.dependents(component))

    def to_json(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "limitation": RUNTIME_LIMITATION,
            "components": {k: self.components[k] for k in sorted(self.components)},
            "edges": [
                {"from": s, "to": d, "kind": k, "imports": n}
                for (s, d, k), n in sorted(self.edges.items())
            ],
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "ArchitectureGraph":
        g = cls(data["revision"], dict(data["components"]))
        for e in data["edges"]:
            g.edges[(e["from"], e["to"], e["kind"])] = e["imports"]
        return g


def extract(root: Path, revision: str, *, module: str = MODULE, top: Iterable[str] = ("pkg", "cmd", "tests")) -> ArchitectureGraph:
    """Build the component graph from a source tree checked out at ``revision``."""
    graph = ArchitectureGraph(revision)
    root = Path(root)
    for top_dir in top:
        base = root / top_dir
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.go")):
            rel = path.relative_to(root).as_posix()
            if "/vendor/" in f"/{rel}" or "/testdata/" in f"/{rel}":
                continue
            comp = component_of(rel)
            info = graph.components.setdefault(comp, {"files": 0, "test_files": 0, "owner_team": None, "runtime_relationships": "unknown"})
            is_test = rel.endswith("_test.go")
            info["test_files" if is_test else "files"] += 1
            try:
                src = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for imp in parse_imports(src):
                dst = component_of_import(imp, module)
                if dst:
                    graph.add_edge(comp, dst, "test_depends_on" if is_test else "depends_on")
        for owners in sorted(base.rglob("OWNERS")):
            comp = component_of(owners.relative_to(root).as_posix())
            if comp in graph.components and graph.components[comp]["owner_team"] is None:
                graph.components[comp]["owner_team"] = _owner_team(owners.read_text(encoding="utf-8", errors="replace"))
    return graph


def architecture_records(graph: ArchitectureGraph, valid_from: datetime) -> list[tuple[Record, dict[str, Any], str]]:
    """Evidence records for components and edges (source architecture, identity-bound)."""
    out: list[tuple[Record, dict[str, Any], str]] = []

    def rec(source_id: str, view: dict[str, Any], etype: str) -> None:
        version = hashlib.sha256(json.dumps(view, sort_keys=True).encode()).hexdigest()[:16]
        out.append((
            Record(
                record_id=f"architecture:{source_id}@{version}",
                source="architecture",
                source_id=source_id,
                revision=None,
                content_version_id=version,
                valid_from=valid_from,
                observed_at=valid_from,
                confirmed_through=valid_from,
                writer=AUTHORITATIVE_WRITERS["architecture"],
                provenance=Provenance.DERIVED,
                synthetic_ancestry=False,
                access_scopes=frozenset({"public"}),
                lineage=(),
                content_ref=f"db:architecture:{source_id}@{version}",
            ),
            view,
            etype,
        ))

    for name, info in sorted(graph.components.items()):
        rec(f"component/{name}", {"component": name, "revision": graph.revision, **info, "limitation": RUNTIME_LIMITATION}, "component")
    for (s, d, k), n in sorted(graph.edges.items()):
        rec(f"edge/{k}/{s}->{d}", {"from": s, "to": d, "kind": k, "imports": n, "revision": graph.revision}, "dependency_edge")
    return out


class ReleaseGraph:
    """Knowledge graph over one snapshot, built only from that snapshot's store."""

    def __init__(self, store: EvidenceStore, ctx: RunContext) -> None:
        self.ctx = ctx
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: set[tuple[str, str, str]] = set()
        self._arch = ArchitectureGraph(revision="")
        for r in store.search(ctx, {"source": "architecture", "limit": 100_000}):
            view = store.content(ctx, r.record_id) or {}
            if view.get("kind") in ("depends_on", "test_depends_on"):
                self._arch.edges[(view["from"], view["to"], view["kind"])] = view.get("imports", 1)
                self.edges.add((f"component:{view['from']}", f"component:{view['to']}", view["kind"]))
            elif "component" in view:
                self._arch.components[view["component"]] = view
                self.nodes[f"component:{view['component']}"] = {"type": "component", "owner_team": view.get("owner_team"), "record_id": r.record_id}
                if view.get("owner_team"):
                    self.edges.add((f"component:{view['component']}", f"team:{view['owner_team']}", "owned_by_team"))
        for r in store.search(ctx, {"source": "issues", "limit": 100_000}):
            v = store.content(ctx, r.record_id) or {}
            nid = f"issue:{v.get('number')}"
            self.nodes[nid] = {"type": "issue", "record_id": r.record_id, "labels": v.get("labels", [])}
            if v.get("duplicate_of"):
                self.edges.add((nid, f"issue:{v['duplicate_of']}", "duplicates"))
        for r in store.search(ctx, {"source": "git", "limit": 100_000}):
            v = store.content(ctx, r.record_id) or {}
            if "number" in v:
                nid = f"pr:{v['number']}"
            elif "sha" in v:
                nid = f"commit:{v['sha']}"
            else:
                continue
            self.nodes[nid] = {"type": nid.split(":")[0], "record_id": r.record_id}
            for f in v.get("files", []):
                self.edges.add((nid, f"component:{component_of(f)}", "touches"))
        for r in store.search(ctx, {"source": "ci", "limit": 100_000}):
            v = store.content(ctx, r.record_id) or {}
            self.nodes[f"ci_job:{v.get('job')}/{v.get('build_id')}"] = {"type": "ci_job", "record_id": r.record_id}

    @property
    def architecture(self) -> ArchitectureGraph:
        return self._arch

    def trace_dependency_impact(self, component: str) -> dict[str, Any]:
        return {
            "component": component,
            "depends_on": sorted(self._arch.depends_on(component)),
            "dependents": sorted(self._arch.dependents(component)),
            "runtime_relationships": "unknown",
            "limitation": RUNTIME_LIMITATION,
        }

    def touching(self, component: str) -> list[str]:
        return sorted(s for (s, d, k) in self.edges if d == f"component:{component}" and k == "touches")

    def to_json(self) -> dict[str, Any]:
        return {
            "nodes": [{"id": k, **v} for k, v in sorted(self.nodes.items())],
            "edges": [{"from": s, "to": d, "kind": k} for (s, d, k) in sorted(self.edges)],
            "limitation": RUNTIME_LIMITATION,
        }


def dependency_proof_cases(graph: ArchitectureGraph, n: int = 10) -> list[dict[str, str]]:
    """Proof cases from real extracted edges: a failing component whose cause is one edge away."""
    by_fan_in = sorted(
        ((s, d) for (s, d, k) in graph.edges if k == "depends_on" and s.startswith("pkg/") and d.startswith("pkg/")),
        key=lambda e: (-graph.fan_in(e[1]), e),
    )
    cases, used = [], set()
    for s, d in by_fan_in:
        if s in used:
            continue
        used.add(s)
        cases.append({"failing_component": s, "cause_component": d})
        if len(cases) == n:
            break
    return cases


def group_by_component(paths: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for p in paths:
        counts[component_of(p)] += 1
    return dict(counts)
