"""Role-based navigation: the one table that says which pages each role sees, in what order, and
which home layout it gets. The web interface renders only what ``GET /api/navigation`` returns; the
API still refuses data a role may not read, so the menu is a convenience, never the control.

The table is checked against the trigger catalog at import time (and in the tests), so a role that
is shown "Run now" always has at least one trigger it may start.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Request

from atlas.api.catalog import MANUAL_TRIGGERS
from atlas.api.identity import principal_of
from atlas.api.support import envelope

router = APIRouter()

ROLES = ("pm", "em", "architect", "security", "oncall", "audit")

ROLE_TITLES = {
    "pm": "product manager",
    "em": "engineering manager",
    "architect": "solution architect",
    "security": "security engineer",
    "oncall": "on-call engineer",
    "audit": "audit role",
}


@dataclass(frozen=True)
class Page:
    page_id: str
    path: str
    title: str
    group: str
    purpose: str  # "who this page is for", in one line


PAGES: tuple[Page, ...] = (
    Page("home", "/", "Home", "Overview", "Your starting point: what needs your attention in this project."),
    Page("dashboard", "/dashboard", "Live dashboard", "Overview", "Everyone: what changed in the project's public sources."),
    Page("report_forecast", "/latest-report/forecast", "Forecast report", "Reports",
         "For the product manager: the debt and risks the next release will inherit."),
    Page("report_allocation", "/latest-report/allocation", "Allocation report", "Reports",
         "For the engineering manager: who should take each item."),
    Page("report_architecture", "/latest-report/architecture", "Architecture report", "Reports",
         "For the solution architect: hotspots and debt to accept or fix."),
    Page("report_security", "/latest-report/security", "Security gate report", "Reports",
         "For the security engineer: the gate result and blocking findings."),
    Page("report_investigation", "/latest-report/investigation", "Investigation reports", "Reports",
         "For on-call: the cause of a failure, with its evidence."),
    Page("run_now", "/run-now", "Run now", "Work", "For people who start runs by hand."),
    Page("schedules", "/schedules", "Automatic runs", "Work", "For the engineering manager: watchers and schedules."),
    Page("runs", "/runs", "Runs", "Work", "Every run with its status and reports."),
    Page("queue", "/queue", "Needs a person", "Work", "Items ATLAS will not decide on its own."),
    Page("approvals", "/approvals", "Approvals", "Work", "For the engineering manager: approve or reject coding-agent tasks."),
    Page("tasks", "/tasks", "Tasks", "Work", "For the engineering manager: coding-agent tasks from proposal to acceptance."),
    Page("notifications", "/notifications", "Notifications", "Safety", "Errors, reminders and decisions routed to your role."),
    Page("containment", "/containment", "Kill switches", "Safety",
         "For security (read only) and audit (resume): switches that stop parts of ATLAS."),
    Page("system", "/system", "System and mode", "Safety", "For the audit role: health, mode, incidents and failed deliveries."),
    Page("scorecard", "/scorecard", "Scorecard", "Insight", "For the engineering manager: did the coding agent help?"),
    Page("graph", "/graph", "Knowledge graph", "Insight", "For the solution architect: components and their import links."),
    Page("feedback", "/feedback", "Record a decision", "Insight", "Overrides and debt-acceptance decisions."),
    Page("evaluation", "/evaluation", "Quality checks", "Insight", "The quality bars ATLAS must pass."),
    Page("access_requests", "/access-requests", "Access requests", "Safety",
         "Your requests to open locked items; security and audit also decide on sensitive ones."),
    Page("demo", "/demo", "Guided demo", "Demo", "A walk through every part of ATLAS."),
)
BY_ID = {p.page_id: p for p in PAGES}
GROUP_ORDER = ("Overview", "Reports", "Work", "Safety", "Insight", "Demo")

# The matrix. Order inside a role's list is the menu order within each group.
ROLE_PAGES: dict[str, tuple[str, ...]] = {
    "pm": ("home", "dashboard", "report_forecast", "run_now", "runs", "notifications", "access_requests", "feedback",
           "evaluation"),
    "em": ("home", "dashboard", "report_allocation", "queue", "approvals", "tasks", "schedules", "run_now", "runs",
           "notifications", "access_requests", "scorecard"),
    "architect": ("home", "dashboard", "report_architecture", "runs", "notifications", "access_requests", "graph",
                  "feedback"),
    "security": ("home", "dashboard", "report_security", "queue", "notifications", "containment", "access_requests"),
    "oncall": ("home", "dashboard", "report_investigation", "run_now", "runs", "notifications", "access_requests"),
    "audit": tuple(p.page_id for p in PAGES),
}

ROLE_HOME = {r: f"{r}_home" for r in ROLES}

# What "Run now" offers each role is decided by the trigger catalog; the menu only shows the page
# to roles that may start at least one trigger.
TRIGGER_ROLES = {r for t in MANUAL_TRIGGERS for r in t.roles}


def check_matrix() -> None:
    """Raises if the menu and the trigger catalog disagree, or a page id is unknown."""
    for role, pages in ROLE_PAGES.items():
        unknown = [p for p in pages if p not in BY_ID]
        if unknown:
            raise ValueError(f"unknown pages for {role}: {unknown}")
        if "home" not in pages or "dashboard" not in pages:
            raise ValueError(f"{role} must see home and the live dashboard")
        if "run_now" in pages and role not in TRIGGER_ROLES:
            raise ValueError(f"{role} sees Run now but may start no trigger")
    reverify_roles = next(t.roles for t in MANUAL_TRIGGERS if t.trigger_id == "reverify")
    for role in reverify_roles:
        if "runs" not in ROLE_PAGES.get(role, ()):
            raise ValueError(f"{role} may re-check runs but cannot see Runs")


check_matrix()


def roles_for(page_id: str) -> list[str]:
    return [r for r in ROLES if page_id in ROLE_PAGES[r]]


def navigation_for(role: str | None) -> dict[str, Any]:
    pages = ROLE_PAGES.get(role or "", ("home", "dashboard"))
    groups = []
    for g in GROUP_ORDER:
        items = [BY_ID[p] for p in pages if BY_ID[p].group == g]
        if items:
            groups.append({"title": g, "pages": [
                {"page_id": p.page_id, "path": p.path, "title": p.title, "for": p.purpose} for p in items]})
    hidden = [
        {"page_id": p.page_id, "path": p.path, "title": p.title, "for": p.purpose,
         "roles": roles_for(p.page_id), "role_titles": [ROLE_TITLES[r] for r in roles_for(p.page_id)]}
        for p in PAGES if p.page_id not in pages
    ]
    return {
        "role": role,
        "role_title": ROLE_TITLES.get(role or "", "visitor"),
        "home": ROLE_HOME.get(role or "", "basic_home"),
        "groups": groups,
        "other_pages": hidden,
    }


@router.get("/navigation")
def navigation(request: Request) -> dict[str, Any]:
    principal = principal_of(request)
    role = principal.role.value if principal.role else None
    body = navigation_for(role)
    body["project_id"] = principal.project_id
    return envelope(request, principal, body)
