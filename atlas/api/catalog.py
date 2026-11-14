"""The manual trigger table. The catalog endpoint and admission read this one table, so a button the
UI shows as disabled is also refused by the endpoint behind it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from atlas.contracts import Mode, Principal


@dataclass(frozen=True)
class ManualTrigger:
    trigger_id: str
    method: str
    path: str
    title: str
    roles: frozenset[str]
    parameters: tuple[tuple[str, str, bool], ...]  # (name, type, required)
    needs_model: bool = True


MANUAL_TRIGGERS: tuple[ManualTrigger, ...] = (
    ManualTrigger("release_close", "POST", "/api/triggers/release-close", "Run release close",
                  frozenset({"pm", "em", "audit"}),
                  (("snapshot_id", "string", True), ("release_id", "string", True), ("snapshot_as_of", "date-time", False))),
    ManualTrigger("investigate", "POST", "/api/triggers/manual/investigate", "Investigate a failure or issue",
                  frozenset({"em", "oncall", "architect", "audit"}),
                  (("snapshot_id", "string", True), ("event_id", "string", True), ("summary", "string", False))),
    ManualTrigger("nightly", "POST", "/api/triggers/nightly", "Run the nightly sweep",
                  frozenset({"pm", "em", "audit"}),
                  (("snapshot_id", "string", True), ("queued", "array<string>", False))),
    ManualTrigger("question", "POST", "/api/triggers/question", "Ask a question",
                  frozenset({"pm", "em", "architect", "security", "oncall", "audit"}),
                  (("snapshot_id", "string", True), ("question", "string", True))),
    ManualTrigger("reverify", "POST", "/api/runs/{run_id}/reverify", "Re-check a stored run",
                  frozenset({"em", "audit"}), (("run_id", "string", True),)),
)
BY_ID = {t.trigger_id: t for t in MANUAL_TRIGGERS}


def availability(trigger: ManualTrigger, principal: Principal, *, mode: Mode,
                 permits: Callable[[str], Any], model_configured: bool) -> tuple[bool, str | None]:
    """``(available, reason)`` for one trigger and caller. The same check runs at admission."""
    role = principal.role.value if principal.role else None
    if role not in trigger.roles:
        return False, "role"
    if trigger.needs_model:
        if mode == Mode.OFFLINE:
            return False, "offline"
        allowed = permits("model_calls")
        if not getattr(allowed, "allowed", True):
            reasons = getattr(allowed, "reasons", ()) or ("containment",)
            text = "; ".join(reasons)
            # A drill names its kill switch directly; other reasons keep the containment prefix.
            return False, text if text.startswith("kill switch ") else "containment: " + text
        if not model_configured:
            return False, "missing configuration: model key"
    return True, None
