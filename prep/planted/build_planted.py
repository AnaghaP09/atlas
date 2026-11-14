"""Build the planted verifier set from the case snapshots.

``python -m prep.planted.build_planted --records <records.jsonl> --out data/evaluator/planted/planted.json``

For each case in ``spec.json`` this resolves the cited records in that case's snapshot and writes
one true finding (expected verdict ``supported``) and one wrong finding (expected ``rejected``)
that cites a real record which does not support it. The output lives in the evaluator area, which
the serving process cannot read.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

SPEC = Path(__file__).with_name("spec.json")


class PlantedError(ValueError):
    pass


def _match(record: Mapping[str, Any], selector: Mapping[str, Any]) -> bool:
    return all(str(record.get(k)) == str(v) for k, v in selector.items())


def _resolve(records: Sequence[Mapping[str, Any]], case: str, selector: Mapping[str, Any]) -> str:
    hits = [r for r in records if r.get("case") == case and _match(r, selector)]
    if not hits:
        hits = [r for r in records if _match(r, selector)]
    if len(hits) != 1:
        raise PlantedError(f"{case}: expected exactly one record for {dict(selector)}, found {len(hits)}")
    return str(hits[0]["record_id"])


def build(records: Sequence[Mapping[str, Any]], spec: Mapping[str, Any]) -> dict[str, Any]:
    true_findings, wrong_findings = [], []
    for entry in spec["cases"]:
        case = entry["case"]
        support = _resolve(records, case, entry["true"]["support"])
        cited = _resolve(records, case, entry["wrong"]["cite"])
        if cited == support:
            raise PlantedError(f"{case}: the wrong finding must cite a different record")
        true_findings.append({"id": f"true-{case}", "case": case, "text": entry["true"]["text"],
                              "evidence_ids": [support], "expected": "supported"})
        wrong_findings.append({"id": f"wrong-{case}", "case": case, "text": entry["wrong"]["text"],
                               "evidence_ids": [cited], "expected": "rejected"})
    if len(true_findings) != 10 or len(wrong_findings) != 10:
        raise PlantedError("the planted set needs exactly 10 true and 10 wrong findings")
    return {"true": true_findings, "wrong": wrong_findings}


def load_records(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    data = json.loads(text)
    return data if isinstance(data, list) else data["records"]


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("data/evaluator/planted/planted.json"))
    args = parser.parse_args(list(argv) if argv is not None else None)
    result = build(load_records(args.records), json.loads(SPEC.read_text(encoding="utf-8")))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"wrote {len(result['true'])} true and {len(result['wrong'])} wrong findings to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
