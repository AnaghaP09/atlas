"""Run the quality checks and write one evaluation record.

    python -m eval.acceptance --plan                                  # what will run, expected cost and time
    python -m eval.acceptance --run --session <folder> --env-file .env
    python -m eval.claim_check --session <folder>                     # a person checks the published claims
    python -m eval.proxy_label                                        # a person labels the blocker candidates
    python -m eval.acceptance --record --session <folder> --env-file .env

``--run`` makes real model calls and stops at the spend ceiling (``--max-usd``, default from the
settings). Every run's output is kept in the session folder, so a stopped run continues where it
stopped. ``--record`` scores what the session holds and writes an evaluation record that cannot be
changed afterwards; the Quality checks screen reads it. A check with missing inputs is reported as
not measured, never as passed. ``--sample N`` runs only N example cases once, to try the plumbing:
its record is marked as a sample and is never shown as the latest evaluation.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from eval import checks, claim_check, gates, proxy_label
from eval.harness import Harness, SpendCeilingReached, report_of

ROOT = Path(__file__).resolve().parents[1]
ALIAS_MAP = ROOT / "data" / "evaluator" / "alias_map.json"
INJECTION = ROOT / "data" / "evaluator" / "injection_expected.csv"
ARCHITECTURE = ROOT / "data" / "snapshots" / "architecture" / "v1.9.0.json"
REAL_REPS = ("R1", "R2", "R3")
ALIASED_REPS = ("A1", "A2", "A3")
SAMPLE_ORDER = ("G03", "G07", "G06", "G01", "G02", "G04", "G05")
PUBLISHED_REPS = ("R1", "A1")  # only these are published; the other repetitions are measurements
NOT_BY_THIS_RUN = "not measured by this run; the automated tests cover it"
# Typical figures from earlier real runs, used only for the plan.
COST = {"investigation": 0.25, "release_close": 0.10, "injection": 0.15, "question": 0.01, "planted": 0.09}
MINUTES = {"investigation": 3.0, "release_close": 1.0, "injection": 1.5, "question": 0.2, "planted": 1.0}
HUMAN_MINUTES = {"claim": 0.5, "blocker_candidate": 1.0, "task": 10.0}


def _alias_event(event: str) -> str:
    if not ALIAS_MAP.exists():
        return event
    numbers = json.loads(ALIAS_MAP.read_text(encoding="utf-8")).get("maps", {}).get("number", {})
    return str(numbers.get(str(event), event))


def golden_steps(golden: Mapping[str, Mapping[str, str]], reps: Sequence[str], cases: Sequence[str] | None = None) -> list[dict[str, Any]]:
    steps = []
    for rep in reps:
        aliased = rep.startswith("A")
        for gid, row in golden.items():
            if cases is not None and gid not in cases:
                continue
            kind = row["kind"]
            snapshot = row["snapshot_id"]
            event = row.get("event_id", "")
            if aliased and kind in ("investigation", "release_close"):
                snapshot += "-aliased"
                if kind == "investigation":
                    event = _alias_event(event)
            steps.append({"golden_id": gid, "rep": rep, "kind": kind, "snapshot_id": snapshot, "event_id": event})
    return steps


def plan(sample: int = 0, repetitions: int = 3) -> dict[str, Any]:
    golden = checks.golden_expected()
    if sample:
        steps = golden_steps(golden, ("R1",), SAMPLE_ORDER[:sample])
        extra: list[dict[str, Any]] = [{"kind": "planted", "count": 1}]
    else:
        steps = golden_steps(golden, REAL_REPS[:repetitions] + ALIASED_REPS[:repetitions])
        extra = [{"kind": "planted", "count": 1}, {"kind": "injection", "count": 4, "what": "copies with a hidden instruction"},
                 {"kind": "investigation", "count": 5, "what": "copies with one new record"},
                 {"kind": "release_close", "count": 4, "what": "forecasts at four earlier releases"}]
    counts: dict[str, int] = {}
    for s in steps:
        counts[s["kind"]] = counts.get(s["kind"], 0) + 1
    for e in extra:
        counts[e["kind"]] = counts.get(e["kind"], 0) + e["count"]
    usd = sum(COST[k] * n for k, n in counts.items())
    minutes = sum(MINUTES[k] * n for k, n in counts.items())
    candidates = len(proxy_label.load_candidates())
    human = [] if sample else [
        {"step": "check published claims", "tool": "python -m eval.claim_check --session <folder>",
         "items": "about 140 claims", "minutes": round(140 * HUMAN_MINUTES["claim"])},
        {"step": "label blocker candidates", "tool": "python -m eval.proxy_label", "items": f"{candidates} candidates",
         "minutes": round(candidates * HUMAN_MINUTES["blocker_candidate"])},
    ]
    return {"sample": bool(sample), "runs": counts, "expected_usd": round(usd, 2), "expected_minutes": round(minutes),
            "human_steps": human, "checked_by": claim_check.CHECKER,
            "not_measured_by_this_run": ["outbound connections", "access control", "kill switches",
                                         "coding task accepted (needs a published task)"]}


def print_plan(p: Mapping[str, Any]) -> None:
    print("Sample run (plumbing only, not a result)." if p["sample"] else "Full evaluation.")
    for kind, n in p["runs"].items():
        print(f"  {n:3d} x {kind.replace('_', ' ')}")
    print(f"Expected model cost: about {p['expected_usd']:.2f} USD. Expected run time: about {p['expected_minutes']} minutes.")
    for h in p["human_steps"]:
        print(f"Person: {h['step']} ({h['items']}), about {h['minutes']} minutes, with `{h['tool']}`.")
    print("Not measured by this run: " + "; ".join(p["not_measured_by_this_run"]) + ".")


# ---------------------------------------------------------------- run

def _save(session: Path, name: str, data: Any) -> None:
    session.mkdir(parents=True, exist_ok=True)
    (session / name).write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")


def _load(session: Path, name: str) -> Any:
    path = session / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _execute(h: Harness, step: Mapping[str, Any]) -> dict[str, Any]:
    kind = step["kind"]
    if kind == "release_close":
        return h.release_close(step["snapshot_id"], step["event_id"], step["rep"])
    if kind == "question":
        return h.question(step["snapshot_id"], checks.PEOPLE_QUESTION, step["rep"])
    return h.investigate(step["snapshot_id"], step["event_id"], step["rep"])


def run(h: Harness, session: Path, *, sample: int = 0, repetitions: int = 3) -> dict[str, Any]:
    golden = checks.golden_expected()
    steps = (golden_steps(golden, ("R1",), SAMPLE_ORDER[:sample]) if sample
             else golden_steps(golden, REAL_REPS[:repetitions] + ALIASED_REPS[:repetitions]))
    state: dict[str, Any] = {"sample": sample, "repetitions": 1 if sample else repetitions, "stopped": None,
                             "started_at": datetime.now(timezone.utc).isoformat(), "steps": steps}
    _save(session, "plan.json", state)
    try:
        for step in steps:
            out = _execute(h, step)
            print(f"{step['rep']} {step['golden_id']}: {out.get('run_status') or out.get('error')}", flush=True)
        if _load(session, "planted.json") is None:
            _save(session, "planted.json", checks.run_planted(h))
            print("planted findings: done", flush=True)
        if not sample:
            for row in checks.read_csv(INJECTION):
                if row["variant"] == "injected":
                    h.investigate("injection-" + row["twin_id"].lower(), golden[row["base_case"]]["event_id"])
            _save(session, "new_evidence.json", checks.run_new_evidence(h))
            for release in proxy_label.RELEASES:  # forecasts at the four earlier releases
                major, minor = release.split(".")
                previous = f"v{major}.{int(minor) - 1}.0"
                h.release_close(f"release-close-{previous}", previous)
    except SpendCeilingReached as exc:
        state["stopped"] = str(exc)
        print(f"Stopped: {exc}. Run again with a higher ceiling to continue.", flush=True)
    published = {k: v for k, v in h.cache.items.items()
                 if k.rsplit("|", 1)[-1] in PUBLISHED_REPS and isinstance(v, dict) and v.get("run_id")}
    _save(session, "claims.json", claim_check.collect(published, h.evidence))
    state["finished_at"] = datetime.now(timezone.utc).isoformat()
    state["model_spend_usd"] = round(h.cache.spend(), 4)
    _save(session, "plan.json", state)
    return state


# ---------------------------------------------------------------- record

def _report_for(runs: Mapping[str, Any], step: Mapping[str, Any]) -> dict[str, Any] | None:
    kind = step["kind"]
    if kind == "release_close":
        key = f"release_close|{step['snapshot_id']}|{step['event_id']}|{step['rep']}"
        run = runs.get(key)
        return report_of(run, "forecast") if run and run.get("reports") else None
    if kind == "question":
        key = f"question|{step['snapshot_id']}|{checks.PEOPLE_QUESTION}|{step['rep']}"
    else:
        key = f"investigate|{step['snapshot_id']}|{step['event_id']}|{step['rep']}"
    run = runs.get(key)
    return report_of(run, "investigation", "question") if run and run.get("reports") else None


def _note(gate: dict[str, Any], note: str) -> dict[str, Any]:
    gate["note"] = note
    return gate


def _dependency_proof() -> dict[str, int] | None:
    if not ARCHITECTURE.exists():
        return None
    from atlas.graph import ArchitectureGraph, dependency_proof_cases

    graph = ArchitectureGraph.from_json(json.loads(ARCHITECTURE.read_text(encoding="utf-8")))
    cases = dependency_proof_cases(graph, 10)
    return {"cases": len(cases), "found": sum(1 for c in cases if c["cause_component"] in graph.one_hop(c["failing_component"]))}


def build_record(session: Path, labels_path: Path = proxy_label.LABELS) -> dict[str, Any]:
    """Score what the session holds. Missing inputs give "unevaluated", never "passed"."""
    state = _load(session, "plan.json") or {}
    runs = _load(session, "runs.json") or {}
    golden = checks.golden_expected()
    sample = bool(state.get("sample"))
    steps = state.get("steps") or []
    out: dict[str, Any] = {}

    # Example cases
    repetitions: dict[str, dict[str, Any]] = {}
    for step in steps:
        report = _report_for(runs, step)
        if report is not None:
            repetitions.setdefault(step["rep"], {})[step["golden_id"]] = report
    correct = {rep: sum(gates.case_correct(o.get(g), golden[g]) for g in o) for rep, o in repetitions.items()}
    reps_needed = set(REAL_REPS + ALIASED_REPS)
    if sample or not reps_needed <= set(repetitions):
        ran = sum(len(o) for o in repetitions.values())
        why = (f"sample run: {sum(correct.values())} of {ran} example cases correct in one repetition; not a result" if sample
               else f"{len(repetitions)} of 6 repetitions recorded")
        out["G-1"] = gates.result("unevaluated", correct, [why])
    else:
        safety = {rep: {"G09": {"hitl_entry": o.get("G09", {}).get("safety_disposition") == "HOLD_FOR_HUMAN",
                                "side_effects": []}} for rep, o in repetitions.items()}
        out["G-1"] = gates.g1(repetitions, golden, safety)

    planted = _load(session, "planted.json")
    out["G-2"] = planted["gate"] if planted else gates.unevaluated("planted findings not run")

    claims = _load(session, "claims.json") or []
    out["G-3"] = claim_check.claim_support(claims, claim_check.read_checks(session / "claim_checks.csv"))

    # Hidden instructions
    injected, clean, expected = {}, {}, {}
    for row in checks.read_csv(INJECTION):
        case = row["base_case"]
        event = golden[case]["event_id"] if case in golden else ""
        if row["variant"] == "clean":
            run = runs.get(f"investigate|case-{case.lower()}|{event}|R1")
            expected[case] = {"expected_classification": row["expected_classification"],
                              "expected_action": row["expected_action"]}
            if run and run.get("reports"):
                clean[case] = report_of(run, "investigation")
        else:
            snapshot = "injection-" + row["twin_id"].lower()
            run = runs.get(f"investigate|{snapshot}|{event}|R1")
            expected.setdefault(case, {"expected_classification": "UNKNOWN", "expected_action": "REJECT"})
            if run and run.get("reports"):
                injected[case] = report_of(run, "investigation")
    out["G-4"] = (gates.g4(injected, clean, expected) if len(injected) == 5 and len(clean) == 4
                  else gates.unevaluated("hidden-instruction copies not all run" + (" (sample run)" if sample else "")))

    # Limits, speed and cost
    investigations = [{"run_id": r.get("run_id"), "minutes": float((r.get("cost") or {}).get("seconds") or 0) / 60, "events": []}
                      for k, r in runs.items() if k.startswith("investigate|") and r.get("run_id")]
    closes = [float((r.get("cost") or {}).get("seconds") or 0) / 60 for k, r in runs.items()
              if k.startswith("release_close|") and r.get("run_id")]
    g5 = gates.g5(investigations, closes)
    out["G-5"] = (_note(gates.result("unevaluated", g5["counts"], g5["failures"]), "sample run; step-by-step budget events are not captured")
                  if sample else _note(g5, "timing only; step-by-step budget events are covered by the automated tests"))
    out["G-6"] = gates.unevaluated(NOT_BY_THIS_RUN)
    measured = [r for r in runs.values() if isinstance(r, dict) and r.get("run_id")]
    out["G-7"] = gates.g7(measured)

    # Carry-over forecast
    close = runs.get("release_close|release-close-v1.9.0|v1.9.0|R1")
    if close and close.get("reports"):
        reports = close["reports"]
        forecast = reports.get("forecast", {})
        items = (forecast.get("forecast") or {}).get("items") if isinstance(forecast.get("forecast"), Mapping) else None
        items = items or forecast.get("items") or []
        by_role = {"pm": items, "em": reports.get("allocation", {}).get("items") or [],
                   "architect": reports.get("architecture", {}).get("items") or []}
        out["G-8"] = gates.g8([{"issue_id": i.get("issue_id") or i.get("id")} for i in items], by_role)
    else:
        out["G-8"] = gates.unevaluated("release close not run" + (" (sample run)" if sample else ""))

    out["G-9"] = gates.unevaluated("no coding task has been published and accepted yet")

    # Stage proofs
    new_evidence = _load(session, "new_evidence.json")
    atlas_results = {}
    for gid, row in golden.items():
        if row["kind"] == "investigation":
            run = runs.get(f"investigate|{row['snapshot_id']}|{row['event_id']}|R1")
            if run:
                atlas_results[gid] = run
    for row in checks.read_csv(checks.NEW_EVIDENCE):
        base_event = golden[row["base_case"]]["event_id"]
        run = runs.get(f"investigate|new-evidence-{row['case_id'].lower()}|{base_event}|R1")
        if run:
            atlas_results[row["case_id"]] = run
    comparison = [c for c in checks.run_comparison(atlas_results, checks.comparison_cases(golden)) if c["case"] in atlas_results]
    _save(session, "comparison.json", comparison)
    if sample or new_evidence is None or len(comparison) < 12:
        out["G-10"] = gates.result("unevaluated", {"comparison_cases": len(comparison)},
                                   ["stage proofs need the full run" if sample else "new-evidence cases or comparison incomplete"])
    else:
        out["G-10"] = gates.g10(dependency=_dependency_proof(), new_evidence=new_evidence["changed"],
                                comparison=comparison, fix_verified=None)
    out["G-11"] = gates.unevaluated(NOT_BY_THIS_RUN)
    out["G-12"] = gates.unevaluated(NOT_BY_THIS_RUN)

    # Forecast accuracy on the substitute blocker definition
    state_labels = proxy_label.blockers(labels_path)
    closes_ready = all(f"release_close|release-close-v1.{n}.0|v1.{n}.0|R1" in runs for n in (5, 6, 7, 8))
    if not state_labels["complete"]:
        out["G-13"] = gates.result("unevaluated", {"labelled": state_labels["labelled"], "candidates": state_labels["candidates"]},
                                   [f"{state_labels['missing']} of {state_labels['candidates']} blocker candidates "
                                    f"(open when the forecast was made) still to be labelled by a person"],
                                   limitation=gates.score_g13([])["limitation"], note=gates.score_g13([])["note"])
    elif not closes_ready:
        out["G-13"] = gates.unevaluated("forecasts at the four earlier releases not run")
    else:
        closes_in = []
        for release in proxy_label.RELEASES:
            major, minor = release.split(".")
            previous = f"v{major}.{int(minor) - 1}.0"
            report = report_of(runs[f"release_close|release-close-{previous}|{previous}|R1"], "forecast")
            fc = report.get("forecast") if isinstance(report.get("forecast"), Mapping) else {}
            items = [{"issue_id": i.get("issue_id") or i.get("id")} for i in (fc.get("items") or report.get("items") or [])]
            closes_in.append({"release": release, "items": items, "blockers": state_labels["blockers"].get(release, []),
                              "all_blockers": state_labels["all_blockers"].get(release, [])})
        out["G-13"] = gates.g13(closes_in)
        out["G-13"]["checked_by"] = proxy_label.CHECKED_BY

    statuses = [g["status"] for g in out.values()]
    overall = ("sample" if sample else "failed" if "failed" in statuses else
               "passed" if all(s == "passed" for s in statuses) else "incomplete")
    return {"gates": out, "overall": overall, "sample": sample,
            "label": "Sample run to try the plumbing. Not an evaluation result." if sample else "Evaluation",
            "checked_by": claim_check.CHECKER, "completed_at": datetime.now(timezone.utc).isoformat(),
            "model_spend_usd": state.get("model_spend_usd"), "stopped": state.get("stopped"),
            "comparison": comparison, "new_evidence": (new_evidence or {}).get("detail")}


def _revision() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def record(h: Harness, session: Path) -> str:
    """Write the evaluation record. A sample is stored with status "sample", so it is never the latest evaluation."""
    result = build_record(session)
    _save(session, "summary.json", result)
    state = _load(session, "plan.json") or {}
    corpus = {"steps": state.get("steps"), "sample": result["sample"], "repetitions": state.get("repetitions")}
    status = "sample" if result["sample"] else "complete"
    return h.services.evaluations.add(result, code_revision=_revision(), corpus=corpus, status=status)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eval.acceptance", description=__doc__.split("\n\n")[0])
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--record", action="store_true")
    parser.add_argument("--session", type=Path)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--sample", type=int, default=0)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--max-usd", type=float, default=0.0, help="spend ceiling for this evaluation")
    args = parser.parse_args(argv)
    if args.plan or not (args.run or args.record):
        print_plan(plan(args.sample, args.repetitions))
        return 0
    if args.session is None:
        parser.error("--run and --record need --session")
    h = Harness.in_process(args.env_file, data_dir=args.data_dir, session_dir=args.session, max_usd=args.max_usd)
    if args.run:
        state = run(h, args.session, sample=args.sample, repetitions=args.repetitions)
        print(f"Model spend in this session: {state['model_spend_usd']:.2f} USD.")
    if args.record:
        evaluation_id = record(h, args.session)
        summary = _load(args.session, "summary.json")
        for gate_id, g in summary["gates"].items():
            print(f"  {gate_id}: {g['status']}" + (f" ({'; '.join(g['failures'])})" if g.get("failures") else ""))
        print(f"Evaluation record {evaluation_id} written ({summary['label']}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
