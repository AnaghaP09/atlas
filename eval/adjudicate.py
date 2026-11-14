"""Record human adjudications for published claims and proxy-blocker candidates.

Two team members label independently. A claim counts as supported only when both say so for the
same output version and the same evidence versions. For proxy-blocker candidates the product manager
settles a disagreement, and both original labels are kept.

    python -m eval.adjudicate claims  --who <name> --items claims.json --out data/local/adjudications.csv
    python -m eval.adjudicate proxy   --who <name> --items candidates.csv --out data/local/proxy_labels.csv

The output files stay under data/local/ and are never committed.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

CLAIM_FIELDS = ("claim_id", "output_version", "evidence_versions", "adjudicator", "label", "note", "at")
PROXY_FIELDS = ("release", "issue_id", "adjudicator", "label", "note", "at")


def append_rows(path: Path, fields: Sequence[str], rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with open(path, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(fields))
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({k: (json.dumps(r[k]) if isinstance(r.get(k), list) else r.get(k, "")) for k in fields})


def load_claim_adjudications(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["evidence_versions"] = json.loads(r["evidence_versions"] or "[]")
    return rows


def resolve_proxy(labels: Sequence[Mapping[str, Any]], pm_decisions: Mapping[tuple[str, str], str]) -> dict[tuple[str, str], dict[str, Any]]:
    """Final proxy-blocker labels per (release, issue) with both labels and the PM's decision when needed."""
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for l in labels:
        grouped.setdefault((l["release"], l["issue_id"]), []).append(l)
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for key, ls in grouped.items():
        votes = {l["adjudicator"]: l["label"] for l in ls}
        if len(votes) < 2:
            out[key] = {"final": None, "labels": votes, "status": "incomplete"}
        elif len(set(votes.values())) == 1:
            out[key] = {"final": next(iter(votes.values())), "labels": votes, "status": "agreed"}
        elif key in pm_decisions:
            out[key] = {"final": pm_decisions[key], "labels": votes, "status": "settled_by_pm"}
        else:
            out[key] = {"final": None, "labels": votes, "status": "disputed"}
    return out


def _prompt(items: Sequence[Mapping[str, Any]], ask: Callable[[str], str], describe: Callable[[Mapping[str, Any]], str],
            choices: Sequence[str]) -> list[tuple[Mapping[str, Any], str, str]]:
    answers = []
    for item in items:
        print(describe(item))
        label = ""
        while label not in choices:
            label = ask(f"label {'/'.join(choices)}: ").strip().lower()
        note = ask("note (optional): ").strip()
        answers.append((item, label, note))
    return answers


def main(argv: list[str] | None = None, ask: Callable[[str], str] = input) -> int:
    parser = argparse.ArgumentParser(prog="python -m eval.adjudicate")
    parser.add_argument("kind", choices=["claims", "proxy"])
    parser.add_argument("--who", required=True)
    parser.add_argument("--items", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc).isoformat()
    if args.kind == "claims":
        items = json.loads(args.items.read_text(encoding="utf-8"))
        answers = _prompt(items, ask, lambda c: f"\n{c['claim_id']}: {c['text']}\nevidence: {c.get('evidence_text', '')}",
                          ("supported", "unsupported"))
        append_rows(args.out, CLAIM_FIELDS, [
            {"claim_id": c["claim_id"], "output_version": c["output_version"], "evidence_versions": c["evidence_versions"],
             "adjudicator": args.who, "label": label, "note": note, "at": now} for c, label, note in answers])
    else:
        with open(args.items, encoding="utf-8", newline="") as f:
            items = list(csv.DictReader(f))
        answers = _prompt(items, ask, lambda c: f"\n{c['release']} {c['issue_id']}: {c.get('title', '')} ({c.get('reason', '')})",
                          ("blocker", "not_blocker"))
        append_rows(args.out, PROXY_FIELDS, [
            {"release": c["release"], "issue_id": c["issue_id"], "adjudicator": args.who, "label": label, "note": note,
             "at": now} for c, label, note in answers])
    return 0


if __name__ == "__main__":
    sys.exit(main())
