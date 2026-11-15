"""Check every published claim against the record it cites. One person does this.

    python -m eval.claim_check --session <folder>            # check claims, one at a time; stop any time
    python -m eval.claim_check --session <folder> --status   # progress only

The evaluation writes ``claims.json`` into its session folder: every claim ATLAS published, with
the text of the records it cites. Claims come in this order: runs where ATLAS was least sure
first (lowest confidence, then most claims rejected by Sentinel). For each claim answer ``y`` (the
record states or directly shows the claim), ``n`` (it does not), ``s`` (skip) or ``q`` (stop).
Answers go to ``claim_checks.csv`` in the same folder. A skipped claim is asked again next time.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from eval import gates

FIELDS = ("claim_id", "output_version", "evidence_versions", "checker", "label", "note", "at")
CHECKER = "one person"
TEXT_LIMIT = 1500


def _text_of(record: Mapping[str, Any] | None) -> str:
    if not record:
        return "(the record could not be read)"
    for key in ("text", "content", "excerpt", "summary"):
        value = record.get(key)
        if value:
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            return text[:TEXT_LIMIT] + (" [cut]" if len(text) > TEXT_LIMIT else "")
    return json.dumps(record, ensure_ascii=False, default=str)[:TEXT_LIMIT]


def collect(runs: Mapping[str, Mapping[str, Any]], evidence_of: Callable[[str, str], Mapping[str, Any] | None]) -> list[dict[str, Any]]:
    """Every published claim of the given runs, with the text of the records it cites."""
    claims = []
    for label, run in runs.items():
        run_id = run.get("run_id")
        for report_type, report in (run.get("reports") or {}).items():
            listed = report.get("claims") or []
            rejected = int((report.get("verifier") or {}).get("claims_rejected") or 0)
            for claim in listed:
                evidence = []
                versions = []
                for record_id in claim.get("evidence_ids", []):
                    record = evidence_of(str(run_id), str(record_id))
                    evidence.append({"record_id": record_id, "text": _text_of(record)})
                    versions.append(str((record or {}).get("content_version_id") or record_id))
                claims.append({
                    "claim_id": f"{run_id}:{report_type}:{claim.get('id')}", "run": label, "run_id": run_id,
                    "report_type": report_type, "text": claim.get("text", ""), "verdict": claim.get("verdict"),
                    "output_version": str(report.get("report_version", 1)), "evidence_versions": versions,
                    "evidence": evidence, "confidence": report.get("confidence"), "rejected_in_run": rejected,
                })
    return order(claims)


def order(claims: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Least sure first: lowest run confidence, then most claims rejected in the same run."""
    def key(c: Mapping[str, Any]) -> tuple[float, int, str]:
        confidence = c.get("confidence")
        return (float(confidence) if isinstance(confidence, (int, float)) else 0.0, -int(c.get("rejected_in_run") or 0),
                str(c.get("claim_id")))
    return sorted((dict(c) for c in claims), key=key)


def read_checks(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["evidence_versions"] = json.loads(r.get("evidence_versions") or "[]")
    return rows


def _write(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(FIELDS))
        w.writeheader()
        for r in rows:
            w.writerow({k: (json.dumps(r[k]) if k == "evidence_versions" else r.get(k, "")) for k in FIELDS})


def _matches(check: Mapping[str, Any], claim: Mapping[str, Any]) -> bool:
    return (check["claim_id"] == claim["claim_id"] and str(check["output_version"]) == str(claim["output_version"])
            and list(check["evidence_versions"]) == list(claim["evidence_versions"]))


def describe(claim: Mapping[str, Any], position: int, total: int) -> str:
    lines = [f"\n[{position} of {total}] {claim['run']} ({claim['report_type']})", f"  Claim: {claim['text']}"]
    for e in claim["evidence"]:
        lines.append(f"  Cited record {e['record_id']}:")
        lines.append("    " + " ".join(str(e["text"]).split()))
    return "\n".join(lines)


def check(claims: Sequence[Mapping[str, Any]], out: Path, ask: Callable[[str], str] = input,
          show: Callable[[str], None] = print) -> dict[str, int]:
    rows = read_checks(out)
    todo = [c for c in claims if not any(_matches(r, c) for r in rows)]
    counts = {"checked": 0, "skipped": 0, "already": len(claims) - len(todo)}
    for c in todo:
        show(describe(c, counts["already"] + counts["checked"] + counts["skipped"] + 1, len(claims)))
        answer = ""
        while answer not in ("y", "n", "s", "q"):
            answer = ask("Does the record state or directly show the claim? y / n / s (skip) / q (stop): ").strip().lower()
        if answer == "q":
            break
        if answer == "s":
            counts["skipped"] += 1
            continue
        note = ask("Note (optional): ").strip()
        rows.append({"claim_id": c["claim_id"], "output_version": c["output_version"],
                     "evidence_versions": list(c["evidence_versions"]), "checker": CHECKER,
                     "label": "supported" if answer == "y" else "unsupported", "note": note,
                     "at": datetime.now(timezone.utc).isoformat()})
        _write(out, rows)
        counts["checked"] += 1
    return counts


def claim_support(claims: Sequence[Mapping[str, Any]], checks: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The claim-support check with one checker: every published claim has Sentinel's verdict
    "supported" and the person's label "supported" for the same output and evidence versions."""
    if not claims:
        return gates.unevaluated("no published claims recorded")
    failures = []
    unsupported = missing = 0
    for c in claims:
        if c.get("verdict") != "supported":
            unsupported += 1
            failures.append(f"{c['claim_id']}: verdict {c.get('verdict')}")
            continue
        labels = [r["label"] for r in checks if _matches(r, c)]
        if not labels:
            missing += 1
        elif labels[-1] != "supported":
            unsupported += 1
            failures.append(f"{c['claim_id']}: the person found it unsupported")
    counts = {"claims": len(claims), "unsupported": unsupported, "still_to_check": missing}
    if unsupported:
        return gates.result("failed", counts, failures, checked_by=CHECKER)
    if missing:
        return gates.result("unevaluated", counts, [f"{missing} of {len(claims)} published claims still to be checked by a person"])
    return gates.result("passed", counts, [], checked_by=CHECKER)


def main(argv: list[str] | None = None, ask: Callable[[str], str] = input) -> int:
    parser = argparse.ArgumentParser(prog="python -m eval.claim_check", description=__doc__.split("\n\n")[0])
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args(argv)
    claims_path = args.session / "claims.json"
    if not claims_path.exists():
        print(f"No claims file at {claims_path}. Run the evaluation first.", file=sys.stderr)
        return 2
    claims = json.loads(claims_path.read_text(encoding="utf-8"))
    out = args.session / "claim_checks.csv"
    if not args.status:
        counts = check(claims, out, ask)
        print(f"\nChecked {counts['checked']} now, skipped {counts['skipped']}.")
    result = claim_support(claims, read_checks(out))
    c = result["counts"]
    print(f"{c['claims'] - c['still_to_check']} of {c['claims']} claims checked; {c['unsupported']} unsupported.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
