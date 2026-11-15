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

Open at forecast time. The forecast for release 1.N is made at the close of release 1.(N-1), from
that close's frozen data set. A blocker the forecast could not have seen is not a fair miss, so a
candidate counts only when the issue was open in that data set, taken as it stood at the data set's
cutoff (latest current record per issue with ``valid_from`` at or before the cutoff, state ``OPEN``).
``python -m prep.proxy.build --outcomes`` marks every candidate in ``data/evaluator/release_outcomes``
with ``open_at_forecast``, keeps only the open ones in the labelling queue (the release files), and
moves the others, each with the reason, to ``release_outcomes/not_open_at_forecast.json``. The
evaluator zone keeps both, so the narrowing can be checked. Running it again gives the same result.
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[2]
BUG_KINDS = {"kind/bug", "kind/flake", "bug", "flake"}
TRACKER_WORDS = ("tracker", "quarantine transition")
OUTCOMES = ROOT / "data" / "evaluator" / "release_outcomes"
RELEASES_DIR = ROOT / "data" / "snapshots" / "releases"
LEFT_OUT = "not_open_at_forecast.json"
QUEUE_CSV = ROOT / "data" / "local" / "proxy_candidates.csv"
RULE = ("A candidate for release 1.N counts only when the issue was open in the frozen data set of the "
        "release 1.(N-1) close, the data set the forecast was made from.")
REPORTED_LATER = "reported after the forecast was made"
NOT_IN_DATA_SET = "open then, but not in the forecast data set (not labelled as a bug or flaky test at that time)"
CLOSED_THEN = "closed when the forecast was made"


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


# ---------------------------------------------------------------- open at forecast time

def _time(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def previous_close(release: str) -> str:
    """``1.6`` -> ``release-close-v1.5.0``: the data set the forecast for 1.6 was made from."""
    major, minor = release.split(".")
    return f"release-close-v{major}.{int(minor) - 1}.0"


def forecast_view(snapshot_dir: Path) -> dict[str, str] | None:
    """Issue states in a frozen data set as of its cutoff, by issue number. ``None`` when the data set is absent.

    As-of rules: only current records, only records valid at or before the cutoff, the latest one per issue wins."""
    meta, records = snapshot_dir / "snapshot.json", snapshot_dir / "records.jsonl"
    if not (meta.exists() and records.exists()):
        return None
    cutoff = _time(json.loads(meta.read_text(encoding="utf-8"))["cutoff"])
    latest: dict[str, tuple[datetime, int, str]] = {}
    for line in records.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("entity_type") != "issue" or row.get("status", "current") != "current":
            continue
        valid = _time(row["valid_from"])
        if valid > cutoff:
            continue
        content = json.loads(row["content"]) if isinstance(row.get("content"), str) else (row.get("content") or {})
        number = str(content.get("number", ""))
        key = (valid, int(row.get("seq") or 0), str(content.get("state", "")))
        if number and (number not in latest or key[:2] >= latest[number][:2]):
            latest[number] = key
    return {n: v[2] for n, v in latest.items()}


def why_left_out(candidate: Mapping[str, Any], cutoff: str) -> str:
    created = candidate.get("created_at")
    if created and _time(created) > _time(cutoff):
        return REPORTED_LATER
    if candidate.get("open_at_previous_close"):
        return NOT_IN_DATA_SET
    return CLOSED_THEN


def split_outcome(outcome: Mapping[str, Any], view: Mapping[str, str] | None,
                  earlier_left_out: Sequence[Mapping[str, Any]] = ()) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Mark each candidate ``open_at_forecast``; return the outcome holding only the open ones, and the others.

    ``earlier_left_out`` are candidates a previous run moved aside; they are judged again, so the split is repeatable.
    Without the forecast data set nothing is eligible (the check cannot be made, so nothing counts)."""
    release = outcome["release"]
    data_set = previous_close(release)
    cutoff = (outcome.get("window") or {}).get("forecast_cutoff", "")
    pool: dict[str, dict[str, Any]] = {}
    for c in list(outcome.get("candidates", [])) + list(earlier_left_out):
        c = {k: v for k, v in c.items() if k not in ("open_at_forecast", "why_left_out", "release")}
        pool.setdefault(str(c["issue"]), c)
    kept, left = [], []
    for number in sorted(pool, key=lambda n: int(n) if n.isdigit() else 0):
        c = pool[number]
        is_open = view is not None and view.get(number) == "OPEN"
        c["open_at_forecast"] = is_open
        if is_open:
            kept.append(c)
        else:
            left.append({**c, "release": release, "why_left_out":
                         why_left_out(c, cutoff) if view is not None else f"forecast data set {data_set} not present"})
    out = dict(outcome)
    out["candidates"] = kept
    out["eligibility"] = {"rule": RULE, "forecast_data_set": data_set, "forecast_cutoff": cutoff,
                          "open_at_forecast": len(kept), "not_open_at_forecast": len(left),
                          "left_out_file": LEFT_OUT}
    return out, left


def _dump(path: Path, data: Any) -> None:
    path.write_bytes((json.dumps(data, indent=1, sort_keys=True) + "\n").encode("utf-8"))


def rebuild_outcomes(outcomes_dir: Path = OUTCOMES, releases_dir: Path = RELEASES_DIR,
                     queue_csv: Path | None = QUEUE_CSV) -> dict[str, dict[str, int]]:
    """Apply the open-at-forecast rule to every release outcome file. Returns counts per release."""
    side = outcomes_dir / LEFT_OUT
    earlier = json.loads(side.read_text(encoding="utf-8")) if side.exists() else {}
    earlier_rows = earlier.get("candidates", []) if isinstance(earlier, dict) else []
    counts: dict[str, dict[str, int]] = {}
    left_all: list[dict[str, Any]] = []
    queue: list[dict[str, Any]] = []
    for path in sorted(outcomes_dir.glob("*.json"), key=lambda p: [int(x) for x in p.stem.split(".") if x.isdigit()]):
        if path.name == LEFT_OUT:
            continue
        outcome = json.loads(path.read_text(encoding="utf-8"))
        release = outcome["release"]
        view = forecast_view(releases_dir / previous_close(release))
        mine = [r for r in earlier_rows if r.get("release") == release]
        new, left = split_outcome(outcome, view, mine)
        _dump(path, new)
        left_all += left
        counts[release] = {"candidates": len(new["candidates"]) + len(left), "open_at_forecast": len(new["candidates"]),
                           "not_open_at_forecast": len(left)}
        for c in new["candidates"]:
            queue.append({"release": release, "issue_id": str(c["issue"]), "title": "",
                          "reason": "; ".join(r.get("kind", "") for r in c.get("reasons", []))})
    _dump(side, {"rule": RULE, "note": "Evaluator only. Candidates left out of the labelling queue because the "
                 "issue was not open in the data set the forecast was made from.",
                 "candidates": left_all})
    if queue_csv is not None:
        write(queue, queue_csv)
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m prep.proxy.build")
    parser.add_argument("--history", type=Path, nargs="+", help="release history JSON files")
    parser.add_argument("--outcomes", action="store_true",
                        help="mark the release outcome candidates open at forecast time and rebuild the labelling queue")
    parser.add_argument("--out", type=Path, default=QUEUE_CSV)
    args = parser.parse_args(argv)
    if args.outcomes:
        counts = rebuild_outcomes(queue_csv=args.out)
        for release, n in counts.items():
            print(f"{release}: {n['open_at_forecast']} of {n['candidates']} candidates open when the forecast was made; "
                  f"{n['not_open_at_forecast']} left out")
        total = sum(n["open_at_forecast"] for n in counts.values())
        print(f"{total} candidate(s) in the labelling queue, also written to {args.out}")
        return 0
    if not args.history:
        parser.error("give --history files or --outcomes")
    rows = [c for p in args.history for c in candidates(json.loads(p.read_text(encoding="utf-8")))]
    print(f"{write(rows, args.out)} candidate(s) written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
