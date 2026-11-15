"""Scout's instructions keep claims to what a record shows and stay free of case answers."""

from __future__ import annotations

import csv
from pathlib import Path

from atlas.agents import investigator

ROOT = Path(__file__).resolve().parents[2]


def test_claims_restate_records_and_leave_recommendations_out():
    text = investigator.SYSTEM
    assert "only restates what a record says" in text
    assert "No recommendations or fixes in claims" in text
    assert "the action is chosen from them" in text
    assert "No claims about absence" in text and "missing_evidence" in text
    assert "keep their hedges" in text


def test_instructions_name_no_component_or_number_from_the_golden_cases():
    text = investigator.SYSTEM.lower()
    for word in ("passt", "fedora", "ipv6", "ipv4", "masquerade", "kubevirt"):
        assert word not in text, word
    with (ROOT / "data" / "evaluator" / "golden_expected.csv").open(encoding="utf-8") as fh:
        events = [row["event_id"] for row in csv.DictReader(fh) if row["event_id"].isdigit()]
    assert events
    for number in events:
        assert number not in text, number
