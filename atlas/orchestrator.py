"""Run orchestration: trigger -> guardrail -> router -> Scout -> services -> Sentinel -> verified artefact.

The orchestrator owns the run store, where every run keeps one authoritative verification
generation. Re-verification writes generation n+1, makes it authoritative, voids every approval
bound to a lower generation, and hands each affected task a support assessment; how a task is
disposed of depends on its publication phase and is applied by the execution layer.
"""

from __future__ import annotations

from atlas.contracts import TaskDisposition  # noqa: F401

import json
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from atlas.agents import SCOUT, SENTINEL
from atlas.agents.investigator import Budget, Investigator, build_tools
from atlas.agents.resolver import EvidenceResolver
from atlas.agents.verifier import Verifier, VerifierFailed
from atlas.contracts import RunContext, RunMode, StartedBy
from atlas.guardrail import InjectionClassifier, flag_instructions, validate_trigger
from atlas.hitl import HumanQueue, triggers_for_run
from atlas.outbound import PayloadWithheld
from atlas.router import BACKLOG_ALERT, route
from atlas.services.allocation import Flags, allocate
from atlas.services.disposition import apply_injection_disposition
from atlas.services.forecast import OpenItem, forecast
from atlas.verified import DanglingReference, build_verified
from atlas.sqlite_util import connect as safe_connect

QUEUE_TO_EVENT = {
    "security_blocked": "security_gate.blocked",
    "security_not_run": "security_gate.not_run",
    "injection_detected": "injection.detected",
    "verifier_rejections": "verifier.rejections_high",
    "run_unverified": "run.unverified",
    "budget_exceeded": "run.over_budget",
    "requirements_at_risk": "requirements.at_risk",
    "forecast_slip": "forecast.slip_over_week",
    "nightly_backlog": "nightly.backlog_high",
}


class RunStore:
    """Verification generations per run; exactly one is authoritative."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._db = safe_connect(str(path), isolation_level=None, check_same_thread=False)
        self._db.executescript(
            "CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, context TEXT, trigger TEXT, started_by TEXT, raw TEXT);"
            "CREATE TABLE IF NOT EXISTS generations (run_id TEXT, generation INTEGER, artefact TEXT, authoritative INTEGER,"
            " created TEXT, PRIMARY KEY (run_id, generation));"
        )
        self.lock = threading.RLock()

    def save_run(self, run_id: str, ctx: RunContext, trigger: Mapping[str, Any], started_by: Mapping[str, Any],
                 raw: Mapping[str, Any]) -> None:
        c = {k: (v.isoformat() if isinstance(v, datetime) else str(v) if v is not None else None) for k, v in asdict(ctx).items()}
        self._db.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?)",
                         (run_id, json.dumps(c), json.dumps(dict(trigger), default=str), json.dumps(dict(started_by)),
                          json.dumps(dict(raw), default=str)))

    def run(self, run_id: str) -> dict[str, Any] | None:
        row = self._db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if not row:
            return None
        c = json.loads(row[1])
        ctx = RunContext(run_id=c["run_id"], snapshot_id=c["snapshot_id"], mode=RunMode(c["mode"]),
                         release_or_case=c["release_or_case"], cutoff=datetime.fromisoformat(c["cutoff"]),
                         alias_variant=c["alias_variant"], evidence_clock=datetime.fromisoformat(c["evidence_clock"]),
                         project_id=c.get("project_id") or "")
        return {"ctx": ctx, "trigger": json.loads(row[2]), "started_by": json.loads(row[3]), "raw": json.loads(row[4])}

    def authoritative(self, run_id: str) -> tuple[int, dict[str, Any]] | None:
        row = self._db.execute("SELECT generation, artefact FROM generations WHERE run_id = ? AND authoritative = 1",
                               (run_id,)).fetchone()
        return (row[0], json.loads(row[1])) if row else None

    def generation(self, run_id: str, generation: int) -> dict[str, Any] | None:
        row = self._db.execute("SELECT artefact FROM generations WHERE run_id = ? AND generation = ?",
                               (run_id, generation)).fetchone()
        return json.loads(row[0]) if row else None

    def write_generation(self, run_id: str, artefact: Mapping[str, Any]) -> int:
        """Write the next generation and make it authoritative in one transaction."""
        with self.lock:
            cur = self._db.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                row = cur.execute("SELECT COALESCE(MAX(generation), 0) FROM generations WHERE run_id = ?", (run_id,)).fetchone()
                gen = row[0] + 1
                body = dict(artefact, authoritative_generation=gen)
                cur.execute("UPDATE generations SET authoritative = 0 WHERE run_id = ?", (run_id,))
                cur.execute("INSERT INTO generations VALUES (?,?,?,1,?)",
                            (run_id, gen, json.dumps(body, default=str), datetime.now(timezone.utc).isoformat()))
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise
        return gen

    def revert_generation(self, run_id: str, generation: int) -> None:
        """Undo a generation whose dependent updates failed (keeps the previous one authoritative)."""
        with self.lock:
            self._db.execute("DELETE FROM generations WHERE run_id = ? AND generation = ?", (run_id, generation))
            self._db.execute("UPDATE generations SET authoritative = 1 WHERE run_id = ? AND generation = ?", (run_id, generation - 1))


@dataclass
class Deps:
    store: Any
    outbound: Any
    gate: Any
    queue: HumanQueue
    notifier: Any
    events: Any
    approvals: Any
    tasks: TaskDisposition | None = None
    content_of: Callable[[Any], str] | None = None
    components_for: Callable[[Mapping[str, Any]], Sequence[str]] = lambda trigger: trigger.get("components", [])
    open_items_for: Callable[[RunContext], Sequence[OpenItem]] = lambda ctx: []
    use_classifier: bool = False
    budget_factory: Callable[[], Budget] = Budget
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)


def _base(run_id: str, report_type: str, started_by: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "run_id": run_id, "report_id": f"rep-{uuid.uuid4().hex[:10]}", "report_type": report_type,
        "authoritative_generation": 1, "report_version": 1, "correction_version": 0, "superseded_by": None,
        "run_status": "complete", "started_by": dict(started_by), "data_mode": "REAL",
        "classification": {"value": None, "claim_ids": [], "required_scopes": ["public"]},
        "action": {"value": None, "claim_ids": [], "required_scopes": ["public"]},
        "confidence": None, "safety_disposition": "NONE", "evidence_recommendation": None, "root_cause": None,
        "insufficient_evidence": [], "items": [], "forecast": "unavailable", "options": [], "findings": [],
        "claims": [], "security_check": {"state": "not_run", "findings": [], "policy_version": "", "exception_ref": None,
                                         "signoff_required": False, "valid_until": None, "scan_run_ids": []},
        "verifier": {"claims_checked": 0, "claims_rejected": 0, "contradictions": []},
        "restricted_items": 0, "injection_detected": False, "redactions_applied": 0, "egress_attempts": 0,
        "approvals": [], "scorecard_delta": None, "reasoning": [],
        "cost": {"input_tokens": 0, "output_tokens": 0, "usd": 0.0, "steps": 0, "tool_calls": 0, "seconds": 0.0},
        "required_scopes": ["public"],
    }


class Orchestrator:
    def __init__(self, deps: Deps, runs: RunStore | None = None) -> None:
        self.d = deps
        self.runs = runs or RunStore()
        self._run_project: dict[str, str] = {}

    # ------------------------------------------------------------------ helpers

    def _resolver(self, ctx: RunContext) -> EvidenceResolver:
        classifier = InjectionClassifier(self.d.outbound) if self.d.use_classifier else None

        def flagger(text: str) -> list[str]:
            flags = flag_instructions(text)
            if not flags and classifier and classifier.is_instruction_like(text):
                flags = ["classifier"]
            return flags

        return EvidenceResolver(self.d.store, ctx, content_of=self.d.content_of, flagger=flagger)

    def _event(self, name: str, run_id: str, gen: int = 1, agent: str | None = None, **payload: Any) -> None:
        self.d.events.emit(name, run_id=run_id, authoritative_generation=gen, agent=agent, payload=payload)

    def _notify(self, event_type: str, subject: str, text: str, scopes: Sequence[str] = ("internal",)) -> None:
        """Notify about a run; the notification carries the run's project so only its members see it."""
        content = {"text": text, "required_scopes": list(scopes)}
        project_id = self._run_project.get(subject.split(":", 1)[0], "")
        if project_id:
            content["project_id"] = project_id
            self.d.notifier.emit(event_type, subject, [subject], frozenset(scopes), content, project_id=project_id)
        else:
            self.d.notifier.emit(event_type, subject, [subject], frozenset(scopes), content)

    @staticmethod
    def _scopes_for(evidence_ids: Sequence[str], resolver: EvidenceResolver) -> list[str]:
        scopes: set[str] = set()
        for rid in evidence_ids:
            r = resolver.by_id(rid)
            scopes |= set(r.record.access_scopes) if r else {"internal"}
        return sorted(scopes or {"public"})

    # --------------------------------------------------------------------- run

    def run(self, run_id: str, ctx: RunContext, trigger: Mapping[str, Any], started_by: StartedBy) -> dict[str, Any]:
        self._note_usage_start(run_id)
        sb = {"kind": str(started_by.kind), "role": str(started_by.role) if started_by.role else None,
              "watcher_id": started_by.watcher_id}
        if ctx.project_id:
            self._run_project[run_id] = ctx.project_id
        validate_trigger(trigger, ctx)
        r = route(trigger, trigger.get("queued", []))
        if r.run_type == "refused" and r.policy is not None:
            out = _base(run_id, "question", sb)
            out["classification"]["value"] = r.policy.classification
            out["action"]["value"] = r.policy.action
            out["safety_disposition"] = "REJECTED"
            out["options"] = [{"text": r.policy.alternative, "claim_ids": [], "required_scopes": ["public"]}]
            out["reasoning"] = [{"text": r.policy.message, "claim_ids": [], "required_scopes": ["public"]}]
            return self._finish(run_id, ctx, trigger, sb, out, raw={"claims": []})
        if r.run_type == "forecast":
            return self._forecast(run_id, ctx, trigger, sb)
        if r.run_type == "nightly":
            results = []
            for event_id in r.items:
                sub = dict(trigger, trigger="failure", event_id=event_id)
                results.append(self._investigate(f"{run_id}:{event_id}", ctx, sub, sb))
            if r.backlog > BACKLOG_ALERT:
                self.d.queue.add("nightly_backlog", run_id, run_id, f"{r.backlog} failures carried forward", project_id=ctx.project_id)
                self._notify("nightly.backlog_high", run_id, f"{r.backlog} failures carried forward to the next night")
            return {"run_id": run_id, "nightly": [x["run_id"] for x in results], "carried_forward": list(r.carried_forward),
                    "backlog": r.backlog}
        report_type = "question" if r.run_type == "question" else "investigation"
        return self._investigate(run_id, ctx, trigger, sb, report_type)

    def _investigate(self, run_id: str, ctx: RunContext, trigger: Mapping[str, Any], sb: Mapping[str, Any],
                     report_type: str = "investigation") -> dict[str, Any]:
        out = _base(run_id, report_type, sb)
        resolver = self._resolver(ctx)
        self._event("run.status_changed", run_id, agent=SCOUT, status="investigating")
        task = trigger.get("question") or f"Investigate failure event {trigger.get('event_id')} ({trigger.get('summary', '')})"
        scout = Investigator(self.d.outbound, resolver, build_tools(), clock=self.d.clock, budget=self.d.budget_factory(),
                             on_step=lambda info: self._event("run.progress", run_id, agent=SCOUT, **info))
        try:
            inv = scout.run(task)
        except PayloadWithheld:
            out["run_status"] = "failed"
            self.d.queue.add("payload_withheld", run_id, run_id, "a payload could not be made safe", project_id=ctx.project_id)
            self._notify("run.failed", run_id, "Scout stopped: a payload could not be made safe to send")
            return self._finish(run_id, ctx, trigger, sb, out, raw={"claims": []})
        out["cost"].update(steps=inv.steps, tool_calls=inv.tool_calls)
        injection = bool(inv.flagged_records)
        if inv.status == "failed":
            out["run_status"] = "failed"
            self._notify("run.failed", run_id, f"Scout could not complete the investigation ({inv.error})")
            return self._finish(run_id, ctx, trigger, sb, out, raw={"claims": []})
        if inv.status == "partial":
            out["run_status"] = "partial"
            out["injection_detected"] = injection
            self.d.queue.add("budget_exceeded", run_id, run_id, f"{inv.budget_exhausted} budget exhausted", project_id=ctx.project_id)
            self._notify("run.partial", run_id, f"Scout stopped at the {inv.budget_exhausted} budget; the report is partial")
            return self._finish(run_id, ctx, trigger, sb, out, raw={"claims": []})
        res = inv.result or {}
        claims = []
        for c in res.get("claims", []):
            claims.append({"id": c["id"], "text": c["text"], "kind": c["kind"], "evidence_ids": list(c["evidence_ids"]),
                           "depends_on": list(c["depends_on"]), "required_scopes": self._scopes_for(c["evidence_ids"], resolver),
                           "verdict": "missing", "verdict_reason": None})
        by_id = {c["id"]: c for c in claims}

        def scopes(ids: Sequence[str]) -> list[str]:
            s: set[str] = set()
            for i in ids:
                s |= set(by_id.get(i, {}).get("required_scopes", ["internal"]))
            return sorted(s or {"public"})

        cls_ids = [i for i in res.get("classification_claim_ids", []) if i in by_id] or [i for i in res.get("root_cause_claim_ids", []) if i in by_id]
        out["classification"] = {"value": res.get("classification"), "claim_ids": cls_ids, "required_scopes": scopes(cls_ids)}
        out["confidence"] = res.get("confidence")
        flags = Flags(**(res.get("flags") or {}))
        out["action"] = {"value": allocate(res.get("classification"), flags), "claim_ids": cls_ids, "required_scopes": scopes(cls_ids)}
        if res.get("root_cause"):
            rc_ids = [i for i in res.get("root_cause_claim_ids", []) if i in by_id]
            out["root_cause"] = {"text": res["root_cause"], "claim_ids": rc_ids, "required_scopes": scopes(rc_ids)}
        out["findings"] = [{"text": f["text"], "claim_ids": [i for i in f["claim_ids"] if i in by_id],
                            "required_scopes": scopes(f["claim_ids"])} for f in res.get("findings", [])]
        out["insufficient_evidence"] = [{"text": m, "rank": n + 1, "claim_ids": [], "required_scopes": ["public"]}
                                        for n, m in enumerate(res.get("missing_evidence", []))]
        out["claims"] = claims
        raw = json.loads(json.dumps(out, default=str))
        verified = self._verify(run_id, out, resolver)
        if verified is None:
            return self._finish(run_id, ctx, trigger, sb, out, raw=raw)
        verified = apply_injection_disposition(verified, injection)
        if self.d.gate is not None:
            gate = self.d.gate.evaluate_for_run(resolver, self.d.components_for(trigger))
            verified["security_check"] = gate.as_security_check()
        return self._finish(run_id, ctx, trigger, sb, verified, raw=raw)

    def _verify(self, run_id: str, out: dict[str, Any], resolver: EvidenceResolver) -> dict[str, Any] | None:
        self._event("run.progress", run_id, agent=SENTINEL, stage="verifying", claims=len(out["claims"]))
        try:
            result = Verifier(self.d.outbound, resolver.by_id).verify(out["claims"])
        except VerifierFailed as exc:
            out["run_status"] = "unverified"
            out["verifier_error"] = str(exc)[:300]
            for name in ("classification", "action"):
                out[name] = {"value": None, "claim_ids": [], "required_scopes": out[name]["required_scopes"]}
            out.update(root_cause=None, findings=[], options=[], items=[], evidence_recommendation=None)
            reason = str(exc).split(": ", 1)[-1][:160]
            self._notify("verifier.missing", run_id, f"Sentinel could not check the claims; nothing is verified ({reason})")
            return None
        out["claims"] = result.claims
        out["verifier"] = {"claims_checked": result.claims_checked, "claims_rejected": result.claims_rejected,
                           "contradictions": result.contradictions}
        try:
            return build_verified(out).artefact
        except DanglingReference:
            out["run_status"] = "failed"
            self._notify("run.failed", run_id, "Scout produced a dangling claim reference; the run failed")
            return None

    def _forecast(self, run_id: str, ctx: RunContext, trigger: Mapping[str, Any], sb: Mapping[str, Any]) -> dict[str, Any]:
        out = _base(run_id, "forecast", sb)
        resolver = self._resolver(ctx)
        items = list(self.d.open_items_for(ctx))
        for it in items:
            for rid in it.record_ids:
                self._preload(resolver, ctx, rid)
        f = forecast(items, ctx.evidence_clock)
        out["classification"] = {"value": "CARRY_OVER_FORECAST", "claim_ids": [], "required_scopes": ["public"]}
        out["action"] = {"value": "PUBLISH_FORECAST", "claim_ids": [], "required_scopes": ["public"]}
        out["items"] = f["items"]
        out["forecast"] = {"items": f["items"], "requirements_at_risk": f["requirements_at_risk"],
                           "excluded_trackers": f["excluded_trackers"], "date_range": "illustrative",
                           "proxy_note": trigger.get("proxy_note")}
        out["claims"] = [dict(c, verdict="missing", verdict_reason=None) for c in f["claims"]]
        raw = json.loads(json.dumps(out, default=str))
        verified = self._verify(run_id, out, resolver)
        if verified is None:
            return self._finish(run_id, ctx, trigger, sb, out, raw=raw)
        return self._finish(run_id, ctx, trigger, sb, verified, raw=raw)

    def _note_usage_start(self, run_id: str) -> None:
        """Remember the boundary's counters when a run starts, so the run can report its own share."""
        stats = getattr(self.d.outbound, "stats", None)
        if not hasattr(self, "_usage_at_start"):
            self._usage_at_start = {}
        if stats is not None:
            self._usage_at_start[run_id] = (int(stats.input_tokens), int(stats.output_tokens), float(stats.cost_usd),
                                            int(sum(stats.redactions.values())), int(stats.egress_attempts))

    def _finish(self, run_id: str, ctx: RunContext, trigger: Mapping[str, Any], sb: Mapping[str, Any],
                out: dict[str, Any], raw: Mapping[str, Any]) -> dict[str, Any]:
        stats = getattr(self.d.outbound, "stats", None)
        if stats is not None:
            # The boundary counts for the whole process; a run reports only what it added.
            base = getattr(self, "_usage_at_start", {}).pop(run_id, (0, 0, 0.0, 0, 0))
            out["redactions_applied"] = max(int(sum(stats.redactions.values())) - base[3], 0)
            out["egress_attempts"] = max(int(stats.egress_attempts) - base[4], 0)
            out["cost"].update(input_tokens=max(stats.input_tokens - base[0], 0),
                               output_tokens=max(stats.output_tokens - base[1], 0),
                               usd=round(max(stats.cost_usd - base[2], 0.0), 6))
        if ctx.project_id:
            out["project_id"] = ctx.project_id
        self.runs.save_run(run_id, ctx, trigger, sb, raw)
        gen = self.runs.write_generation(run_id, out)
        out["authoritative_generation"] = gen
        for trig, reason in triggers_for_run(out):
            if trig in ("budget_exceeded",):
                continue
            _, created = self.d.queue.add(trig, run_id, run_id, reason, project_id=ctx.project_id)
            if created and trig in QUEUE_TO_EVENT and trig != "run_unverified":
                self._notify(QUEUE_TO_EVENT[trig], run_id, reason)
        self._event("run.status_changed", run_id, gen, status=out.get("run_status"))
        return out

    # --------------------------------------------------------------- reverify

    def reverify(self, run_id: str) -> dict[str, Any]:
        """Write generation n+1, void superseded approvals, and hand tasks their support assessment."""
        stored = self.runs.run(run_id)
        current = self.runs.authoritative(run_id)
        if stored is None or current is None:
            raise KeyError(f"unknown run {run_id}")
        n, _ = current
        raw = json.loads(json.dumps(stored["raw"]))
        resolver = self._resolver(stored["ctx"])
        for c in raw.get("claims", []):
            for rid in c.get("evidence_ids", []):
                self._preload(resolver, stored["ctx"], rid)
        failed = False
        new = self._verify(run_id, raw, resolver) if raw.get("claims") else raw
        if new is None:
            failed = True
            new = raw
        new = dict(new, report_version=1 + int(raw.get("report_version", 1)))
        with self.runs.lock:
            gen = self.runs.write_generation(run_id, new)
            try:
                self.d.approvals.void_superseded(run_id, new_generation=gen)
                if self.d.tasks is not None:
                    action = (new.get("action") or {}).get("value")
                    assessments = {}
                    for t in self.d.tasks.tasks_for_run(run_id):
                        if failed:
                            assessments[t["task_id"]] = "verification_failed"
                        elif action is not None and action == t.get("action"):
                            assessments[t["task_id"]] = "still_supported"
                        else:
                            assessments[t["task_id"]] = "unsupported"
                    self.d.tasks.apply_reverification(run_id, gen, assessments)
            except Exception:
                self.runs.revert_generation(run_id, gen)
                raise
        new["authoritative_generation"] = gen
        self._event("run.generation_changed", run_id, gen, agent=SENTINEL, previous_generation=n)
        self._event("report.superseded", run_id, gen, superseded_generation=n)
        return new

    def _preload(self, resolver: EvidenceResolver, ctx: RunContext, record_id: str) -> None:
        """Make a cited record readable by Sentinel. Citations are record ids, not source ids."""
        get_record = getattr(self.d.store, "get_record", None)
        rec = get_record(ctx, record_id) if get_record else None
        if rec is None:
            source, _, rest = record_id.partition(":")
            source_id = rest.split("@", 1)[0]
            if source and source_id:
                rec = self.d.store.get(ctx, source, source_id)
                if rec is not None and rec.record_id != record_id:
                    rec = None
        if rec is not None:
            resolver.resolve(rec)
