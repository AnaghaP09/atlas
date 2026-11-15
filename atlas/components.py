"""Component map for a repository in any language, from static reading only.

Components come from folders: each top-level folder is a component, except well-known container
folders (``src``, ``lib``, ``pkg``, ``cmd``, ``packages`` and similar), whose sub-folders are the
components. A container with a single sub-folder (``src/<package>``) is opened one level further.
Owner teams come from ``OWNERS`` files (their ``sig/`` or ``area/`` labels) and from ``CODEOWNERS``
entries that name a team (``@org/team``); personal accounts are never used.

Import edges are read from source text without running anything:

* Go: ``import`` declarations (``atlas.graph.parse_imports``), resolved through the module path;
* Python: ``import`` and ``from ... import`` statements, read with the ``ast`` parser (parsing does
  not execute code), resolved to files of the repository, relative imports included;
* JavaScript and TypeScript: ``import ... from``, ``export ... from``, ``import()`` and
  ``require()`` with a relative path, resolved to files of the repository.

Imports of outside packages are not edges. Other languages get folders only, and the map says so.
Runtime relationships (calls over the network, plug-ins loaded by name) are never visible this way.
"""

from __future__ import annotations

import ast
import re
from collections import defaultdict
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping

from atlas.graph import ArchitectureGraph, _owner_team, parse_imports

CONTAINERS = frozenset({"src", "lib", "libs", "pkg", "cmd", "packages", "apps", "internal", "modules", "components",
                        "staging", "crates", "services", "plugins"})
SKIP_DIRS = frozenset({".git", "node_modules", "vendor", "dist", "build", "__pycache__", ".venv", "venv",
                       "third_party", "testdata", "fixtures", "__snapshots__", "coverage"})
LANGUAGE_OF = {".go": "Go", ".py": "Python", ".js": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript",
               ".jsx": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript", ".mts": "TypeScript",
               ".cts": "TypeScript"}
JS_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")
READ_LANGUAGES = frozenset(LANGUAGE_OF.values())
LIMITATION = ("Components are folders. Edges come from import statements read as text (Go, Python, JavaScript "
              "and TypeScript); runtime relationships such as network calls are not visible and are unknown.")


def _is_test(path: str) -> bool:
    p = PurePosixPath(path)
    name = p.name.lower()
    parts = {x.lower() for x in p.parts[:-1]}
    return bool(parts & {"test", "tests", "__tests__", "spec", "e2e"}) or name.endswith(("_test.go", "_test.py")) \
        or name.startswith("test_") or re.search(r"\.(test|spec)\.[cm]?[jt]sx?$", name) is not None


def _skipped(path: str) -> bool:
    return any(part in SKIP_DIRS or part.startswith(".") for part in PurePosixPath(path).parts[:-1])


class Layout:
    """Maps a file path to its component, from the repository's folder tree."""

    def __init__(self, paths: Iterable[str]) -> None:
        self.dirs: dict[str, set[str]] = defaultdict(set)  # folder -> child folders ("" = root)
        for path in paths:
            parts = PurePosixPath(path).parts[:-1]
            for i in range(len(parts)):
                self.dirs["/".join(parts[:i])].add(parts[i])
        self.opened: set[str] = set()  # containers that were opened; files directly inside belong to them
        self.roots = self._roots()

    def _expand(self, folder: str) -> bool:
        name = PurePosixPath(folder).name
        return name in CONTAINERS

    def _roots(self) -> set[str]:
        roots: set[str] = set()

        def visit(folder: str, opened_by_container: bool) -> None:
            children = sorted(self.dirs.get(folder, ()))
            for child in children:
                path = f"{folder}/{child}" if folder else child
                if child in SKIP_DIRS or child.startswith("."):
                    continue
                if self._expand(path) and self.dirs.get(path):
                    self.opened.add(path)
                    visit(path, True)
                elif opened_by_container and len(children) == 1 and self.dirs.get(path):
                    # src/<package>: the only package below a container is opened one level more.
                    roots.add(path)
                    visit(path, False)
                else:
                    roots.add(path)

        visit("", False)
        return roots

    def component_of(self, path: str) -> str:
        parts = PurePosixPath(path).parts[:-1]
        best = "root"
        for i in range(1, len(parts) + 1):
            prefix = "/".join(parts[:i])
            if prefix in self.roots or prefix in self.opened:
                best = prefix
        if best == "root" and parts:
            best = parts[0]
        return best


# ---------------------------------------------------------------- import parsers


def python_imports(source: str, path: str) -> list[str]:
    """Absolute dotted module names imported by one Python file (relative imports resolved)."""
    try:
        tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return []
    package = list(PurePosixPath(path).parts[:-1])  # a module's package is its folder
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - (node.level - 1)] if node.level - 1 <= len(package) else []
                prefix = ".".join(base)
                mod = f"{prefix}.{node.module}" if node.module else prefix
                out.append(f"{_REL}{mod}")
                for alias in node.names:
                    out.append(f"{_REL}{mod}.{alias.name}")
            elif node.module:
                out.append(node.module)
                for alias in node.names:
                    out.append(f"{node.module}.{alias.name}")
    seen: list[str] = []
    for m in out:
        if m not in seen:
            seen.append(m)
    return seen


_REL = "./"  # marks a relative import already turned into a repository path (dots for folders)

_JS_SPEC = re.compile(
    r"""(?:\bimport\s+(?:[^'";]*?\s+from\s+)?|\bexport\s+[^'";]*?\s+from\s+|\brequire\s*\(\s*|\bimport\s*\(\s*)(['"])([^'"\n]+)\1"""
)


def _strip_js_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    return re.sub(r"(?m)(^|[^:'\"\\])//.*$", r"\1", src)


def js_imports(source: str) -> list[str]:
    """Module specifiers named by one JavaScript or TypeScript file."""
    text = _strip_js_comments(source)
    seen: list[str] = []
    for m in _JS_SPEC.finditer(text):
        spec = m.group(2).strip()
        if spec and spec not in seen:
            seen.append(spec)
    return seen


def resolve_js(spec: str, importer: str, files: set[str]) -> str | None:
    """The repository file a relative specifier points at, or None (outside packages are not edges)."""
    if not spec.startswith("."):
        return None
    base = PurePosixPath(importer).parent
    parts: list[str] = []
    for part in (base / spec).parts:
        if part == "..":
            if not parts:
                return None
            parts.pop()
        elif part != ".":
            parts.append(part)
    target = "/".join(parts)
    stem = re.sub(r"\.[cm]?js$", "", target)
    candidates = [target] + [stem + ext for ext in JS_EXTENSIONS] + [f"{target}/index{ext}" for ext in JS_EXTENSIONS]
    return next((c for c in candidates if c in files), None)


def resolve_python(module: str, importer: str, files: set[str], source_roots: Iterable[str]) -> str | None:
    """The repository file a dotted module name points at, or None."""
    if module.startswith(_REL):
        dotted = module[len(_REL):]
        roots = [""]
    else:
        dotted = module
        roots = list(source_roots)
    parts = [p for p in dotted.split(".") if p]
    while parts:
        rel = "/".join(parts)
        for root in roots:
            base = f"{root}/{rel}" if root else rel
            for cand in (f"{base}.py", f"{base}/__init__.py"):
                if cand in files:
                    return cand
        parts.pop()  # "from a.b import name": the name may be a function, not a module
    return None


def _python_roots(files: set[str]) -> list[str]:
    """Folders that hold top-level packages: the repository root and common source folders."""
    roots = [""]
    for cand in ("src", "lib", "python"):
        if any(f.startswith(cand + "/") and f.endswith("/__init__.py") for f in files):
            roots.append(cand)
    return roots


def go_module(go_mod_text: str | None) -> str | None:
    for line in (go_mod_text or "").splitlines():
        if line.startswith("module "):
            return line.split(None, 1)[1].strip()
    return None


# ---------------------------------------------------------------- owners


def codeowner_teams(text: str) -> list[tuple[str, str]]:
    """(path pattern, team) pairs from a CODEOWNERS file. Only ``@org/team`` owners are kept."""
    out = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        pattern, *owners = line.split()
        teams = [o[1:] for o in owners if o.startswith("@") and "/" in o]
        if teams:
            out.append((pattern, teams[0]))
    return out


def _pattern_component(pattern: str, layout: Layout) -> str | None:
    clean = pattern.strip("/").rstrip("*").rstrip("/")
    if not clean or any(ch in clean for ch in "*?["):
        return None
    for root in sorted(layout.roots, key=len, reverse=True):
        if clean == root or clean.startswith(root + "/") or root.startswith(clean + "/"):
            return root if (clean == root or clean.startswith(root + "/")) else None
    return None


# ---------------------------------------------------------------- the map


def build_component_map(revision: str, entries: list[Mapping[str, Any]], texts: Mapping[str, str], *,
                        go_mod_text: str | None = None, owners_texts: Mapping[str, str] | None = None) -> tuple[ArchitectureGraph, dict[str, Any]]:
    """The component graph of one revision.

    ``entries`` is the repository tree (``path``, ``type``); ``texts`` holds the text of the source
    files that were read; ``owners_texts`` the OWNERS and CODEOWNERS files by path. Returns the graph
    and a summary (languages read, files read, what was left out).
    """
    files = {str(e["path"]) for e in entries if e.get("type") == "blob" and not _skipped(str(e["path"]))}
    layout = Layout(files)
    graph = ArchitectureGraph(revision)
    langs: dict[str, int] = defaultdict(int)
    for path in sorted(files):
        lang = LANGUAGE_OF.get(PurePosixPath(path).suffix.lower())
        if lang is None:
            continue
        langs[lang] += 1
        comp = layout.component_of(path)
        info = graph.components.setdefault(comp, {"files": 0, "test_files": 0, "owner_team": None,
                                                  "runtime_relationships": "unknown", "edges_from": "imports"})
        info["test_files" if _is_test(path) else "files"] += 1
    for root in layout.roots:
        graph.components.setdefault(root, {"files": 0, "test_files": 0, "owner_team": None,
                                           "runtime_relationships": "unknown", "edges_from": "folders only"})
    module = go_module(go_mod_text)
    py_roots = _python_roots(files)
    for path, text in sorted(texts.items()):
        if path not in files:
            continue
        lang = LANGUAGE_OF.get(PurePosixPath(path).suffix.lower())
        src = layout.component_of(path)
        kind = "test_depends_on" if _is_test(path) else "depends_on"
        targets: list[str] = []
        if lang == "Go" and module:
            for imp in parse_imports(text):
                if imp.startswith(module + "/"):
                    targets.append(layout.component_of(imp[len(module) + 1:] + "/x.go"))
        elif lang == "Python":
            for mod in python_imports(text, path):
                hit = resolve_python(mod, path, files, py_roots)
                if hit:
                    targets.append(layout.component_of(hit))
        elif lang in ("JavaScript", "TypeScript"):
            for spec in js_imports(text):
                hit = resolve_js(spec, path, files)
                if hit:
                    targets.append(layout.component_of(hit))
        for dst in dict.fromkeys(t for t in targets if t):
            graph.add_edge(src, dst, kind)
    for owners_path, text in sorted((owners_texts or {}).items()):
        name = PurePosixPath(owners_path).name
        if name == "OWNERS":
            team = _owner_team(text)
            comp = layout.component_of(owners_path)
            if team and comp in graph.components and graph.components[comp]["owner_team"] is None:
                graph.components[comp]["owner_team"] = team
        elif name == "CODEOWNERS":
            for pattern, team in codeowner_teams(text):
                comp = _pattern_component(pattern, layout)
                if comp and comp in graph.components:
                    graph.components[comp]["owner_team"] = team  # later lines win, as in CODEOWNERS
    unread = sorted(lang for lang in _languages_seen(files) if lang not in READ_LANGUAGES)
    summary = {"components": len(graph.components), "edges": len(graph.edges), "languages_read": dict(sorted(langs.items())),
               "files_read": len(texts), "other_files": unread,
               "note": LIMITATION + (f" Files in other languages ({', '.join(unread)}) give folders only." if unread else "")}
    for info in graph.components.values():
        if info.get("files", 0) + info.get("test_files", 0) == 0:
            info["edges_from"] = "folders only"
    return graph, summary


_OTHER = {".rs": "Rust", ".java": "Java", ".kt": "Kotlin", ".rb": "Ruby", ".php": "PHP", ".cs": "C#", ".c": "C",
          ".cc": "C++", ".cpp": "C++", ".h": "C", ".swift": "Swift", ".scala": "Scala"}


def _languages_seen(files: set[str]) -> set[str]:
    return {_OTHER[PurePosixPath(f).suffix.lower()] for f in files if PurePosixPath(f).suffix.lower() in _OTHER}


def source_paths(entries: list[Mapping[str, Any]], limit: int) -> tuple[list[str], int]:
    """Source files to read for import edges: non-test files first, then tests, at most ``limit``."""
    files = [str(e["path"]) for e in entries if e.get("type") == "blob" and not _skipped(str(e["path"]))
             and PurePosixPath(str(e["path"])).suffix.lower() in LANGUAGE_OF and int(e.get("size") or 0) <= 400_000
             and not str(e["path"]).endswith(".d.ts")]
    ordered = sorted(files, key=lambda p: (_is_test(p), p))
    return ordered[:limit], max(0, len(ordered) - limit)


def owners_paths(entries: list[Mapping[str, Any]]) -> list[str]:
    return sorted(str(e["path"]) for e in entries if e.get("type") == "blob"
                  and PurePosixPath(str(e["path"])).name in ("OWNERS", "CODEOWNERS")
                  and (str(e["path"]).startswith(".github/") or not _skipped(str(e["path"]))))
