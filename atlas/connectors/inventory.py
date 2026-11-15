"""Dependency lists for the OSV scan, read from a repository's lock or module file.

Each reader is static parsing only: nothing from the repository is installed, built or run.

* ``go.mod``: Go modules (ecosystem ``Go``);
* ``package-lock.json``: npm packages (ecosystem ``npm``), lock file versions 1 to 3;
* ``uv.lock`` and ``poetry.lock``: Python packages (ecosystem ``PyPI``);
* ``requirements*.txt``: Python packages pinned with ``==`` (ecosystem ``PyPI``).

A file that is not one of these gives no list, and the scan says so instead of guessing.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import PurePosixPath

from atlas.connectors.security import module_inventory_from_go_mod

# Checked in this order; the first file present in the repository is used.
INVENTORY_FILES = ("go.mod", "package-lock.json", "uv.lock", "poetry.lock", "requirements.txt")

_REQ_LINE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*==\s*([A-Za-z0-9._+!-]+)")


@dataclass(frozen=True)
class Inventory:
    file: str
    ecosystem: str
    packages: tuple[tuple[str, str], ...]

    @property
    def label(self) -> str:
        return f"the {self.ecosystem} package list in {self.file}"


def ecosystem_of(path: str) -> str | None:
    name = PurePosixPath(path).name
    if name == "go.mod":
        return "Go"
    if name == "package-lock.json":
        return "npm"
    if name in ("uv.lock", "poetry.lock") or (name.startswith("requirements") and name.endswith(".txt")):
        return "PyPI"
    return None


def parse_inventory(path: str, text: str) -> Inventory | None:
    """The package list in one file, or None when the file is not a supported list."""
    eco = ecosystem_of(path)
    name = PurePosixPath(path).name
    try:
        if eco == "Go":
            pkgs = module_inventory_from_go_mod(text)
        elif eco == "npm":
            pkgs = _npm_lock(json.loads(text))
        elif name in ("uv.lock", "poetry.lock"):
            pkgs = _toml_lock(tomllib.loads(text))
        elif eco == "PyPI":
            pkgs = _requirements(text)
        else:
            return None
    except (ValueError, TypeError, tomllib.TOMLDecodeError):
        return None
    unique = sorted(set(pkgs))
    return Inventory(path, eco or "", tuple(unique))


def _npm_lock(data: object) -> list[tuple[str, str]]:
    if not isinstance(data, dict):
        return []
    out: list[tuple[str, str]] = []
    packages = data.get("packages")
    if isinstance(packages, dict):  # lock file version 2 and 3
        for key, info in packages.items():
            if not key or not isinstance(info, dict) or info.get("link") or not info.get("version"):
                continue
            name = info.get("name") or key.rsplit("node_modules/", 1)[-1]
            out.append((str(name), str(info["version"])))
        return out
    deps = data.get("dependencies")  # lock file version 1

    def walk(tree: object) -> None:
        if not isinstance(tree, dict):
            return
        for name, info in tree.items():
            if isinstance(info, dict) and info.get("version"):
                out.append((str(name), str(info["version"])))
                walk(info.get("dependencies"))

    walk(deps)
    return out


def _toml_lock(data: dict) -> list[tuple[str, str]]:
    out = []
    for pkg in data.get("package") or []:
        if not isinstance(pkg, dict) or not pkg.get("name") or not pkg.get("version"):
            continue
        source = pkg.get("source") or {}
        if isinstance(source, dict) and (source.get("editable") or source.get("virtual")):
            continue  # the project itself, not a dependency
        out.append((str(pkg["name"]), str(pkg["version"])))
    return out


def _requirements(text: str) -> list[tuple[str, str]]:
    out = []
    for line in text.splitlines():
        m = _REQ_LINE.match(line.split("#", 1)[0])
        if m:
            out.append((m.group(1), m.group(2)))
    return out


def pick_inventory_file(paths: set[str] | list[str]) -> str | None:
    """The first supported list at the top of the repository (then any ``requirements*.txt``)."""
    present = set(paths)
    for name in INVENTORY_FILES:
        if name in present:
            return name
    reqs = sorted(p for p in present if re.match(r"^(requirements/)?requirements[A-Za-z0-9_.-]*\.txt$|^requirements/[A-Za-z0-9_.-]+\.txt$", p))
    return reqs[0] if reqs else None
