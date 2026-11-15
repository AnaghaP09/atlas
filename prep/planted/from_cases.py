"""Build the planted set for the verifier check from the example-case data sets.

    python -m prep.planted.from_cases

For each entry in ``spec.json`` this writes one true finding, which cites the issue record that
states it, and one wrong finding, which cites a real record from the same data set that does not
support it. Half of the wrong findings cite the issue of the case; the other half cite an
unrelated commit from the same data set. The output is ``data/evaluator/planted/planted.json``.
The serving process never reads that folder.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[2]
SPEC = Path(__file__).with_name("spec.json")
CASES = ROOT / "data" / "snapshots" / "cases"
OUT = ROOT / "data" / "evaluator" / "planted" / "planted.json"


class PlantedError(ValueError):
    pass


def snapshot_of(case: str) -> str:
    """``G04b`` belongs to the data set ``case-g04``."""
    return "case-" + case[:3].lower()


def load_rows(case_dir: Path) -> list[dict[str, Any]]:
    text = (case_dir / "records.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def issue_record(rows: Sequence[Mapping[str, Any]], number: str) -> str:
    hits = [r for r in rows if r.get("source") == "issues" and r.get("source_id") == f"issue/{number}"
            and r.get("status", "current") == "current"]
    if len(hits) != 1:
        raise PlantedError(f"expected one current record for issue {number}, found {len(hits)}")
    return str(hits[0]["record_id"])


def unrelated_commit(rows: Sequence[Mapping[str, Any]]) -> str:
    commits = sorted((r for r in rows if r.get("entity_type") == "commit"), key=lambda r: r.get("seq") or 0)
    if not commits:
        raise PlantedError("the data set has no commit record to cite")
    return str(commits[0]["record_id"])


def build(spec: Mapping[str, Any], cases_dir: Path = CASES) -> dict[str, Any]:
    true_findings, wrong_findings = [], []
    cache: dict[str, list[dict[str, Any]]] = {}
    for index, entry in enumerate(spec["cases"]):
        case = entry["case"]
        snapshot_id = snapshot_of(case)
        if snapshot_id not in cache:
            cache[snapshot_id] = load_rows(cases_dir / snapshot_id)
        rows = cache[snapshot_id]
        issue = issue_record(rows, str(entry["true"]["support"]["source_id"]))
        cited = issue if index % 2 == 0 else unrelated_commit(rows)
        true_findings.append({"id": f"true-{case}", "case": case, "snapshot_id": snapshot_id, "kind": "finding",
                              "text": entry["true"]["text"], "evidence_ids": [issue], "expected": "supported"})
        wrong_findings.append({"id": f"wrong-{case}", "case": case, "snapshot_id": snapshot_id, "kind": "finding",
                               "text": entry["wrong"]["text"], "evidence_ids": [cited], "expected": "rejected",
                               "cites": "the issue of the case" if cited == issue else "an unrelated commit"})
    if len(true_findings) != 10 or len(wrong_findings) != 10:
        raise PlantedError("the planted set needs exactly 10 true and 10 wrong findings")
    return {"true": true_findings, "wrong": wrong_findings}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m prep.planted.from_cases")
    parser.add_argument("--cases", type=Path, default=CASES)
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)
    result = build(json.loads(SPEC.read_text(encoding="utf-8")), args.cases)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {len(result['true'])} true and {len(result['wrong'])} wrong findings to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
