"""Label the candidate blocking issues for the forecast accuracy check. One person does this.

The project does not record which issues blocked a release, so the check uses a substitute: an
issue counts as a blocker of release 1.N when it is a bug or flaky test fixed on the release branch
between the first beta and the release, or rated critical-urgent and still open at the release.
The data layer lists the candidates; a person confirms each one.

    python -m eval.proxy_label                 # label the candidates, one at a time; stop any time
    python -m eval.proxy_label --status        # how many are labelled
    python -m eval.proxy_label --provisional   # machine pre-label, for trying the scorer only

Answers: ``y`` (a blocker), ``n`` (not a blocker), ``s`` (skip for now), ``q`` (stop). Labels go
to ``data/evaluator/proxy_blockers.csv`` with "one person" as the checker. A skipped candidate is
asked again next time. The provisional file is never used for a reported result.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
OUTCOMES = ROOT / "data" / "evaluator" / "release_outcomes"
RELEASES_DIR = ROOT / "data" / "snapshots" / "releases"
REFERENCE = ROOT / "data" / "evaluator" / "reference.json"
LABELS = ROOT / "data" / "evaluator" / "proxy_blockers.csv"
PROVISIONAL = ROOT / "data" / "local" / "proxy_blockers.PROVISIONAL.csv"
FIELDS = ("release", "issue_id", "final", "checked_by", "reason", "note", "at")
CHECKED_BY = "one person"
PROVISIONAL_BY = "machine pre-label (provisional, not a result)"
RELEASES = ("1.6", "1.7", "1.8", "1.9")


def previous_close(release: str) -> str:
    major, minor = release.split(".")
    return f"release-close-v{major}.{int(minor) - 1}.0"


def _issue_views(release: str, releases_dir: Path) -> dict[str, dict[str, Any]]:
    """Issues as they stood at the previous release, by number. Empty when the data set is absent."""
    path = releases_dir / previous_close(release) / "records.jsonl"
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("entity_type") == "issue":
            content = json.loads(row["content"])
            out[str(content.get("number"))] = content
    return out


def reason_text(reasons: Sequence[Mapping[str, Any]]) -> str:
    parts = []
    for r in reasons:
        if r.get("kind") == "backport":
            parts.append(f"fixed on the release branch by pull request {r.get('backport_pr')}")
        elif r.get("kind") == "critical_open_at_final_tag":
            parts.append("critical-urgent and open at the release")
        else:
            parts.append(str(r.get("kind")))
    return "; ".join(parts)


def load_candidates(outcomes_dir: Path = OUTCOMES, releases_dir: Path = RELEASES_DIR,
                    releases: Sequence[str] = RELEASES) -> list[dict[str, Any]]:
    repository = ""
    if REFERENCE.exists():
        repository = json.loads(REFERENCE.read_text(encoding="utf-8")).get("project", {}).get("repository", "")
    out = []
    for release in releases:
        path = outcomes_dir / f"{release}.json"
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        views = _issue_views(release, releases_dir)
        backports = {str(b.get("backport_pr")): b.get("title", "") for b in data.get("backports", [])}
        for c in data.get("candidates", []):
            number = str(c["issue"])
            view = views.get(number, {})
            fixes = [backports.get(str(r.get("backport_pr")), "") for r in c.get("reasons", []) if r.get("kind") == "backport"]
            out.append({
                "release": release, "issue_id": number, "reason": reason_text(c.get("reasons", [])),
                "title": view.get("title") or "", "labels": view.get("labels") or c.get("kinds_now") or [],
                "body": str(view.get("body") or "")[:600], "created_at": c.get("created_at"),
                "open_at_previous_release": bool(c.get("open_at_previous_close")),
                "fix_titles": [f for f in fixes if f],
                "link": f"https://github.com/{repository}/issues/{number}" if repository else "",
            })
    return out


def read_labels(path: Path = LABELS) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return [r for r in csv.DictReader(f) if r.get("final")]


def _append(path: Path, row: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = read_labels(path)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(FIELDS))
        w.writeheader()
        for r in rows + [dict(row)]:
            w.writerow({k: r.get(k, "") for k in FIELDS})


def blockers(path: Path = LABELS, candidates: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Final labels for scoring. ``complete`` is true only when a person labelled every candidate."""
    rows = read_labels(path)
    by_release: dict[str, list[str]] = {}
    for r in rows:
        if r["final"] == "blocker":
            by_release.setdefault(r["release"], []).append(r["issue_id"])
    candidates = list(candidates) if candidates is not None else load_candidates()
    labelled = {(r["release"], r["issue_id"]) for r in rows}
    missing = [c for c in candidates if (c["release"], c["issue_id"]) not in labelled]
    by_person = bool(rows) and all(r.get("checked_by") == CHECKED_BY for r in rows)
    return {"blockers": by_release, "labelled": len(labelled), "candidates": len(candidates), "missing": len(missing),
            "checked_by": CHECKED_BY if by_person else (rows[0].get("checked_by") if rows else None),
            "complete": by_person and not missing and bool(candidates)}


def describe(c: Mapping[str, Any], position: int, total: int) -> str:
    lines = [f"\n[{position} of {total}] Release {c['release']}  issue {c['issue_id']}  {c['link']}",
             f"  Why it is a candidate: {c['reason']}"]
    if c["title"]:
        lines.append(f"  Title: {c['title']}")
    lines.append(f"  Labels: {', '.join(c['labels']) or 'none recorded'}")
    lines.append("  Open at the previous release: " + ("yes" if c["open_at_previous_release"] else "no (reported later)"))
    for t in c["fix_titles"]:
        lines.append(f"  Fix on the release branch: {t}")
    if c["body"]:
        lines.append("  Report (start): " + " ".join(c["body"].split())[:400])
    return "\n".join(lines)


def label(candidates: Sequence[Mapping[str, Any]], out: Path = LABELS, ask: Callable[[str], str] = input,
          show: Callable[[str], None] = print) -> dict[str, int]:
    done = {(r["release"], r["issue_id"]) for r in read_labels(out)}
    todo = [c for c in candidates if (c["release"], c["issue_id"]) not in done]
    counts = {"labelled": 0, "skipped": 0, "already": len(done)}
    total = len(candidates)
    for c in todo:
        show(describe(c, total - len(todo) + counts["labelled"] + counts["skipped"] + 1, total))
        answer = ""
        while answer not in ("y", "n", "s", "q"):
            answer = ask("Blocker? y / n / s (skip) / q (stop): ").strip().lower()
        if answer == "q":
            break
        if answer == "s":
            counts["skipped"] += 1
            continue
        note = ask("Note (optional): ").strip()
        _append(out, {"release": c["release"], "issue_id": c["issue_id"],
                      "final": "blocker" if answer == "y" else "not_blocker", "checked_by": CHECKED_BY,
                      "reason": c["reason"], "note": note, "at": datetime.now(timezone.utc).isoformat()})
        counts["labelled"] += 1
    return counts


def write_provisional(candidates: Sequence[Mapping[str, Any]], out: Path = PROVISIONAL) -> int:
    """Mark every candidate a blocker, by machine. For trying the scorer only; never a result."""
    out.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    with open(out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(FIELDS))
        w.writeheader()
        for c in candidates:
            w.writerow({"release": c["release"], "issue_id": c["issue_id"], "final": "blocker",
                        "checked_by": PROVISIONAL_BY, "reason": c["reason"], "note": "", "at": now})
    return len(candidates)


def main(argv: list[str] | None = None, ask: Callable[[str], str] = input) -> int:
    parser = argparse.ArgumentParser(prog="python -m eval.proxy_label", description=__doc__.split("\n\n")[0])
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--provisional", action="store_true")
    parser.add_argument("--out", type=Path, default=LABELS)
    args = parser.parse_args(argv)
    candidates = load_candidates()
    if args.provisional:
        print(f"{write_provisional(candidates)} provisional labels written to {PROVISIONAL}. These are not a result.")
        return 0
    if not args.status:
        counts = label(candidates, args.out, ask)
        print(f"\nLabelled {counts['labelled']} now, skipped {counts['skipped']}.")
    state = blockers(args.out, candidates)
    print(f"{state['labelled']} of {state['candidates']} candidates labelled; {state['missing']} left. "
          f"Checked by {state['checked_by'] or 'nobody yet'}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
