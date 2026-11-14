"""Fetch the source tree of the reference project at a release tag (trusted preparation only).

A shallow, sparse, read-only clone of ``pkg``, ``cmd`` and ``tests`` into ``data/local/src``. The
serving process never fetches source code. No code from the clone is built or run: the
architecture graph comes from static parsing of import statements.

Usage: python -m prep.fetch.source v1.9.0 [more tags]
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from atlas.graph import extract

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "data" / "local" / "src"
EXPORT = ROOT / "data" / "local" / "export" / "snapshots" / "architecture"
URL = "https://github.com/kubevirt/kubevirt.git"


def run(*args: str, cwd: Path | None = None) -> str:
    env = {"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"}
    import os

    full_env = dict(os.environ)
    full_env.update(env)
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "protocol.file.allow=never", *args],
        cwd=cwd, env=full_env, capture_output=True, text=True, check=True,
    ).stdout


def checkout(tag: str) -> tuple[Path, str]:
    dest = SRC / tag
    if not (dest / ".git").exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        run("clone", "--depth", "1", "--branch", tag, "--filter=blob:none", "--sparse", "--no-tags", URL, str(dest))
        run("sparse-checkout", "set", "pkg", "cmd", "tests", cwd=dest)
    sha = run("rev-parse", "HEAD", cwd=dest).strip()
    return dest, sha


def build(tag: str) -> Path:
    src, sha = checkout(tag)
    graph = extract(src, sha)
    EXPORT.mkdir(parents=True, exist_ok=True)
    out = EXPORT / f"{tag}.json"
    data = graph.to_json()
    data["tag"] = tag
    out.write_bytes((json.dumps(data, indent=1, sort_keys=True) + "\n").encode("utf-8"))
    print(tag, sha[:12], "components", len(graph.components), "edges", len(graph.edges), flush=True)
    return out


if __name__ == "__main__":
    for t in sys.argv[1:] or ["v1.9.0"]:
        build(t)
