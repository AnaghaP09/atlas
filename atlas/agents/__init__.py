"""Investigator and verifier agents and their evidence access.

The investigator is shown to people as **Scout** and the verifier as **Sentinel**; both are labelled
as agents, never as people.
"""

SCOUT = "scout"
SENTINEL = "sentinel"
DISPLAY_NAMES = {SCOUT: "Scout", SENTINEL: "Sentinel"}
AGENT_FOR_ROLE = {"investigator": SCOUT, "verifier": SENTINEL}
