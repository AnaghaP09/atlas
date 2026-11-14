"""Build the new-evidence cases: a golden case's records plus one later record that should change the
recommendation. The added record is team-built (synthetic) and is labelled as such; it is dated
after the case's cutoff and before the re-run's evidence clock.

    python -m prep.new_evidence.build --cases <dir of case record files> --out data/snapshots/new_evidence
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[2]
EXPECTED = ROOT / "data" / "evaluator" / "new_evidence_expected.csv"
KIND_FOR_SOURCE = {"git": "commit", "issues": "comment", "kubernetes": "event", "security": "finding"}


def add_record(records: Sequence[Mapping[str, Any]], row: Mapping[str, str], valid_from: str) -> list[dict[str, Any]]:
    text = row["new_record_summary"]
    new = {
        "record_id": f"{row['case_id'].lower()}-new",
        "source": row["new_record_source"],
        "kind": KIND_FOR_SOURCE.get(row["new_record_source"], "note"),
        "text": text,
        "valid_from": valid_from,
        "content_version_id": "ne-" + hashlib.sha256(text.encode()).hexdigest()[:12],
        "provenance": "synthetic",
        "synthetic_ancestry": True,
        "access_scopes": ["restricted:security"] if row["new_record_source"] == "security" else ["public"],
    }
    return [dict(r) for r in records] + [new]


def build(cases_dir: Path, out_dir: Path, valid_from: str) -> list[str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(EXPECTED, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    written = []
    for row in rows:
        records = json.loads((cases_dir / f"{row['base_case']}.json").read_text(encoding="utf-8"))
        (out_dir / f"{row['case_id']}.json").write_text(json.dumps(add_record(records, row, valid_from), indent=2),
                                                         encoding="utf-8")
        written.append(row["case_id"])
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m prep.new_evidence.build")
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "snapshots" / "new_evidence")
    parser.add_argument("--valid-from", default="2026-10-01T00:00:00+00:00")
    args = parser.parse_args(argv)
    print("\n".join(build(args.cases, args.out, args.valid_from)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
