"""Agent reliability scorecard.

Rows per task class record completion (``accepted`` only), checks, active review effort (separate
from approval latency), rework, fully loaded cost and the 30-day defect state; rows are versioned
and updated as later events arrive. Net contribution against a matched human baseline is reported
only when the baseline is valid (real and at least three tasks); otherwise it is ``unavailable``
and any synthetic comparison is shown separately as illustrative. Negative results are shown and
raise a review event.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

from atlas.execution.db import OpsDB, iso

METRICS = ("completed", "review_minutes", "rework", "cost_usd")
LOWER_IS_BETTER = {"review_minutes", "rework", "cost_usd"}


@dataclass(frozen=True)
class BaselineTask:
    task_class: str
    completed: bool
    review_minutes: float
    rework: int
    cost_usd: float
    synthetic: bool = True


def fully_loaded_cost(*, model_usd: float, sandbox_minutes: float, ci_minutes: float, review_minutes: float,
                      hourly_rate_usd: float = 100.0, compute_usd_per_minute: float = 0.01) -> float:
    return round(model_usd + (sandbox_minutes + ci_minutes) * compute_usd_per_minute + review_minutes / 60 * hourly_rate_usd, 4)


class Scorecard:
    def __init__(self, db: OpsDB, notifier: Any = None) -> None:
        self.db = db
        self.notifier = notifier

    def write_row(self, task_id: str, task_class: str, data: Mapping[str, Any], now: datetime) -> int:
        """Append a new version of the task's row; earlier versions are kept."""
        row_id = f"score-{task_id}"
        with self.db.transaction() as conn:
            prev = conn.execute("SELECT MAX(version) FROM scorecard_rows WHERE row_id = ?", (row_id,)).fetchone()[0]
            version = (prev or 0) + 1
            conn.execute("INSERT INTO scorecard_rows (row_id, version, task_id, task_class, data_json, at) VALUES (?,?,?,?,?,?)",
                         (row_id, version, task_id, task_class, json.dumps(dict(data), sort_keys=True, default=str), iso(now)))
        return version

    def latest_rows(self, task_class: str | None = None) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT r.* FROM scorecard_rows r JOIN (SELECT row_id, MAX(version) v FROM scorecard_rows GROUP BY row_id) m"
            " ON r.row_id = m.row_id AND r.version = m.v" + (" WHERE r.task_class = ?" if task_class else "") + " ORDER BY r.at",
            (task_class,) if task_class else (),
        )
        return [{"task_id": r["task_id"], "task_class": r["task_class"], "version": r["version"], **json.loads(r["data_json"])} for r in rows]

    @staticmethod
    def row_data(*, accepted: bool, ci: str, behavioural: str, review_minutes: float, approval_latency_minutes: float,
                 revisions: int, corrective_tasks: int, cost_usd: float, defect_window: str) -> dict[str, Any]:
        return {
            "completed": accepted,
            "ci": ci,
            "behavioural": behavioural,
            "review_minutes": review_minutes,
            "approval_latency_minutes": approval_latency_minutes,
            "rework": revisions + corrective_tasks,
            "cost_usd": cost_usd,
            "defect_window": defect_window,
            "data_mode": "REAL",
        }

    def compare(self, task_class: str, baseline: Sequence[BaselineTask], now: datetime) -> dict[str, Any]:
        agent_rows = self.latest_rows(task_class)
        base = [b for b in baseline if b.task_class == task_class]
        result: dict[str, Any] = {"task_class": task_class, "agent_tasks": len(agent_rows), "baseline_tasks": len(base)}
        if not agent_rows:
            result["net_contribution"] = "unavailable (no agent tasks)"
            return result

        def agent_mean(metric: str) -> float:
            return statistics.fmean(float(r[metric]) for r in agent_rows)

        def comparison(tasks: Sequence[BaselineTask]) -> dict[str, Any]:
            out = {}
            for m in METRICS:
                values = [float(getattr(b, m)) for b in tasks]
                mean = statistics.fmean(values)
                delta = agent_mean(m) - mean
                spread = (min(values) - mean, max(values) - mean)
                better = -delta if m in LOWER_IS_BETTER else delta
                out[m] = {"agent": round(agent_mean(m), 3), "baseline": round(mean, 3), "delta": round(delta, 3),
                          "range": [round(delta - spread[1], 3), round(delta - spread[0], 3)], "better": better > 0}
            return out

        valid = len(base) >= 3 and not any(b.synthetic for b in base)
        if valid:
            result["net_contribution"] = comparison(base)
            negative = [m for m, v in result["net_contribution"].items() if not v["better"] and v["delta"] != 0]
            result["negative_metrics"] = negative
            if negative and self.notifier is not None:
                self.notifier.emit("scorecard.class_negative", task_class, [task_class], frozenset({"public"}))
        else:
            result["net_contribution"] = "unavailable (no valid baseline)"
            if base:
                result["illustrative_comparison"] = {"data_mode": "SYNTHETIC", **comparison(base)}
        return result
