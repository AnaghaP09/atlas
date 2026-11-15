"""Build runnable new-evidence data sets from the example cases.

    python -m prep.new_evidence.snapshots

Each data set is a copy of one example case with one new record: a comment on the issue of the
case that states a later fact. The recommendation for the copy should differ from the
recommendation for the original case. The added comment is made up for the test and marked as
sample data.

Output: ``data/snapshots/new_evidence/<id>/{records.jsonl,conflicts.jsonl,snapshot.json}``, the
same layout as the example cases, listed as test copies.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
EXPECTED = ROOT / "data" / "evaluator" / "new_evidence_expected.csv"
REFERENCE = ROOT / "data" / "evaluator" / "reference.json"
CASES = ROOT / "data" / "snapshots" / "cases"
OUT = ROOT / "data" / "snapshots" / "new_evidence"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def add_comment(rows: list[dict[str, Any]], issue_number: int, text: str, when: str) -> str:
    """Add one comment to the issue of the case, in place. Returns the new record id."""
    row = next(r for r in rows if r["entity_type"] == "issue" and r["source_id"] == f"issue/{issue_number}")
    content = json.loads(row["content"])
    content["comments"] = list(content.get("comments") or []) + [
        {"author": "person-synthetic", "body": text, "body_missing": False, "created_at": when, "id": "new-evidence-1"}]
    body = json.dumps(content, ensure_ascii=False, sort_keys=True)
    row["content"] = body
    row["content_version_id"] = "ne-" + hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]
    row["provenance"] = "synthetic"
    row["synthetic_ancestry"] = 1
    row["record_id"] = str(row["record_id"]).split("@")[0] + "@" + row["content_version_id"][3:]
    row["content_ref"] = "db:" + row["record_id"]
    return str(row["record_id"])


def build(cases_dir: Path = CASES, out_dir: Path = OUT) -> list[dict[str, Any]]:
    reference = json.loads(REFERENCE.read_text(encoding="utf-8"))["golden_cases"]
    with open(EXPECTED, encoding="utf-8", newline="") as f:
        expected = list(csv.DictReader(f))
    built = []
    for item in expected:
        case_dir = cases_dir / f"case-{item['base_case'].lower()}"
        rows = [json.loads(line) for line in (case_dir / "records.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()]
        base = json.loads((case_dir / "snapshot.json").read_text(encoding="utf-8"))
        issue = reference[item["base_case"]]["issues"][0]
        changed = add_comment(rows, issue, item["new_record_summary"], base["cutoff"])
        twin_id = "new-evidence-" + item["case_id"].lower()
        target = out_dir / twin_id
        target.mkdir(parents=True, exist_ok=True)
        records = target / "records.jsonl"
        with open(records, "w", encoding="utf-8", newline="\n") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        conflicts = case_dir / "conflicts.jsonl"
        if conflicts.exists():
            shutil.copyfile(conflicts, target / "conflicts.jsonl")
        else:
            (target / "conflicts.jsonl").write_text("", encoding="utf-8")
        manifest = {
            "snapshot_id": twin_id, "release_or_case": item["base_case"], "cutoff": base["cutoff"],
            "alias_variant": None, "test_copy": "new_evidence", "changed_record": changed, "event_id": str(issue),
            "data_modes": dict(base.get("data_modes", {}), new_record="SYNTHETIC"),
            "notes": ["One made-up comment was added to test whether a new fact changes the recommendation."],
            "records": len(rows), "records_sha256": _sha(records),
        }
        (target / "snapshot.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        built.append({"snapshot_id": twin_id, "base_case": item["base_case"], "event_id": str(issue),
                      "changed_record": changed})
    return built


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m prep.new_evidence.snapshots")
    parser.add_argument("--cases", type=Path, default=CASES)
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)
    for item in build(args.cases, args.out):
        print(item["snapshot_id"], item["base_case"], item["changed_record"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
