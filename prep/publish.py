"""Publish checked exports from ``data/local/export`` into the committed data folders.

Each file is scanned with the repository's public check first; nothing is copied when any file
has a finding. The snapshots and evaluator files are then committed like any other change.

Usage: python -m prep.publish [--dry-run]
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from prep.reference import paths

PUBLISHED = (
    ("snapshots/cases", "snapshots/cases"),
    ("snapshots/cases-aliased", "snapshots/cases-aliased"),
    ("snapshots/releases", "snapshots/releases"),
    ("snapshots/releases-aliased", "snapshots/releases-aliased"),
    ("snapshots/architecture", "snapshots/architecture"),
    ("evaluator/alias_map.json", "evaluator/alias_map.json"),
    ("evaluator/MANIFEST.json", "evaluator/MANIFEST.json"),
    ("evaluator/release_outcomes", "evaluator/release_outcomes"),
)


def _files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    return sorted(p for p in path.rglob("*") if p.is_file()) if path.exists() else []


def scan(files: list[Path]) -> list[str]:
    sys.path.insert(0, str(Path.cwd() / "scripts"))
    import public_check  # the repository's own check

    handles = public_check.load_handles()
    hits: list[str] = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            hits.append(f"{f.as_posix()}: not UTF-8 text")
            continue
        hits += public_check.scan_text(text, f.as_posix(), handles=handles)
    return hits


def main(argv: list[str]) -> int:
    export, data = paths()["export"], paths()["data"]
    pairs = [(export / src, data / dst) for src, dst in PUBLISHED if (export / src).exists()]
    files = [f for src, _ in pairs for f in _files(src)]
    hits = scan(files)
    if hits:
        print(f"{len(hits)} public-check finding(s); nothing published. First ones:")
        for h in hits[:20]:
            print("  ", h)
        return 1
    if "--dry-run" in argv:
        print(f"{len(files)} file(s) would be published")
        return 0
    for src, dst in pairs:
        if src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
        else:
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
    print(f"published {len(files)} file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
