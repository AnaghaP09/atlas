"""Allocation table and staffing options (calculations, not agents)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class Flags:
    bounded_change: bool = False
    cross_component: bool = False
    release_blocker: bool = False
    special_hardware: bool = False
    duplicate_of: str | None = None


def allocate(classification: str | None, flags: Flags) -> str | None:
    """Map a classification plus evidence flags to the recommended action.

    The table is deliberately small and explicit so it can be read and tested on its own.
    """
    if classification is None:
        return None
    if classification == "OUT_OF_SCOPE":
        return "REJECT"
    if classification == "UNKNOWN":
        return "REJECT" if flags.duplicate_of is None else "LINK_DUPLICATE"
    if classification == "DUPLICATE" or flags.duplicate_of:
        return "LINK_DUPLICATE"
    if classification == "CARRY_OVER_FORECAST":
        return "PUBLISH_FORECAST"
    if classification == "ENVIRONMENT":
        return "HAND_TO_INFRA_OWNER"
    if flags.release_blocker:
        return "ESCALATE_DECISION"
    if flags.special_hardware:
        return "ASSIGN_DEVELOPER"
    # Adapting tests is a contained change by nature, so a test defect goes to the coding agent unless
    # it spans components. A dependency goes there only when the fix is a plain update or pin.
    if classification == "TEST_DEFECT" and not flags.cross_component:
        return "DELEGATE_TO_CODING_AGENT"
    if classification == "DEPENDENCY" and flags.bounded_change and not flags.cross_component:
        return "DELEGATE_TO_CODING_AGENT"
    if classification in {"CODE_DEFECT", "DEPENDENCY", "TEST_DEFECT"}:
        return "ASSIGN_DEVELOPER"
    return "ESCALATE_DECISION"


ACTION_ROLE = {
    "DELEGATE_TO_CODING_AGENT": "coding_agent",
    "ASSIGN_DEVELOPER": "developer",
    "ESCALATE_DECISION": "decision_maker",
    "HAND_TO_INFRA_OWNER": "infra_owner",
    "LINK_DUPLICATE": None,
    "PUBLISH_FORECAST": None,
    "REJECT": None,
}


def simulate_staffing(allocation: Sequence[Mapping[str, Any]], capacity: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Return at least three staffing scenarios with load per role and unassigned items; no dates.

    ``allocation`` items: ``{"id", "action", "skills": [...], "effort": units}``.
    ``capacity`` rows (synthetic role ids only): ``{"role_id", "role", "skills": [...], "available": units}``.
    """
    for row in capacity:
        if set(row) - {"role_id", "role", "skills", "available"}:
            raise ValueError("capacity rows may hold only role ids, roles, skills and availability")

    def plan(cap: Sequence[Mapping[str, Any]], agent_units: float) -> dict[str, Any]:
        free = {r["role_id"]: float(r["available"]) for r in cap}
        load: dict[str, float] = {r["role_id"]: 0.0 for r in cap}
        load["coding_agent"] = 0.0
        unassigned: list[str] = []
        agent_left = agent_units
        for item in allocation:
            effort = float(item.get("effort", 1))
            if item.get("action") == "DELEGATE_TO_CODING_AGENT" and agent_left >= effort:
                load["coding_agent"] += effort
                agent_left -= effort
                continue
            if ACTION_ROLE.get(item.get("action")) is None and item.get("action") != "DELEGATE_TO_CODING_AGENT":
                continue
            need = set(item.get("skills", []))
            choice = next((r for r in cap if need <= set(r["skills"]) and free[r["role_id"]] >= effort), None)
            if choice is None:
                unassigned.append(item["id"])
                continue
            free[choice["role_id"]] -= effort
            load[choice["role_id"]] += effort
        return {"load_per_role": load, "unassigned": unassigned}

    delegable = sum(float(i.get("effort", 1)) for i in allocation if i.get("action") == "DELEGATE_TO_CODING_AGENT")
    base = list(capacity)
    borrowed = base + ([{**max(base, key=lambda r: len(r["skills"])), "role_id": "borrowed-1"}] if base else [])
    options = [
        {"name": "current roles", **plan(base, 0.0)},
        {"name": "add coding-agent capacity for delegable items", **plan(base, delegable)},
        {"name": "borrow one role", **plan(borrowed, 0.0)},
    ]
    return {"options": options, "dates": None, "data_mode": "SYNTHETIC"}
