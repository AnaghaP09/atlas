"""List proxy-blocker candidates for the forecast accuracy check.

KubeVirt does not record which issues blocked a release. The proxy counts an issue as a blocker of
release 1.N when it is a bug or flake that is (a) fixed by a pull request merged into ``release-1.N``
between ``v1.N.0-beta.0`` and ``v1.N.0``, or (b) critical-urgent and still open at ``v1.N.0``.
Features, chores, documentation, dependency bumps without a linked bug or flake, and tracker issues
are left out. This script only lists candidates; a person then labels each one with
``python -m eval.proxy_label`` and the labels go into ``data/evaluator/proxy_blockers.csv``.

Input (from the data layer's release history export, one JSON file per release):
``{"release": "1.6", "beta_at": ..., "released_at": ..., "backports": [{"pr", "merged_at", "issues": [
{"issue_id", "kinds": [...], "labels": [...], "title"}]}], "open_at_release": [{"issue_id", "kinds", "labels", "title"}]}``
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[2]
BUG_KINDS = {"kind/bug", "kind/flake", "bug", "flake"}
TRACKER_WORDS = ("tracker", "quarantine transition")


def _eligible(issue: Mapping[str, Any]) -> bool:
    kinds = set(issue.get("kinds", [])) | {l for l in issue.get("labels", []) if l.startswith("kind/")}
    if not kinds & BUG_KINDS:
        return False
    title = str(issue.get("title", "")).lower()
    return not any(w in title for w in TRACKER_WORDS)


def candidates(release_history: Mapping[str, Any]) -> list[dict[str, Any]]:
    release = release_history["release"]
    beta, final = release_history["beta_at"], release_history["released_at"]
    found: dict[str, dict[str, Any]] = {}
    for bp in release_history.get("backports", []):
        if not (beta <= bp["merged_at"] <= final):
            continue
        for issue in bp.get("issues", []):
            if _eligible(issue):
                found.setdefault(str(issue["issue_id"]), {"release": release, "issue_id": str(issue["issue_id"]),
                                                          "title": issue.get("title", ""),
                                                          "reason": f"fixed on the release branch by PR {bp['pr']}"})
    for issue in release_history.get("open_at_release", []):
        if _eligible(issue) and "priority/critical-urgent" in issue.get("labels", []):
            found.setdefault(str(issue["issue_id"]), {"release": release, "issue_id": str(issue["issue_id"]),
                                                      "title": issue.get("title", ""),
                                                      "reason": "critical-urgent and open at release"})
    return sorted(found.values(), key=lambda c: int(c["issue_id"]) if c["issue_id"].isdigit() else 0)


def write(rows: Iterable[Mapping[str, Any]], out: Path) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = list(rows)
    with open(out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["release", "issue_id", "title", "reason"])
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m prep.proxy.build")
    parser.add_argument("--history", type=Path, nargs="+", required=True, help="release history JSON files")
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "local" / "proxy_candidates.csv")
    args = parser.parse_args(argv)
    rows = [c for p in args.history for c in candidates(json.loads(p.read_text(encoding="utf-8")))]
    print(f"{write(rows, args.out)} candidate(s) written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
