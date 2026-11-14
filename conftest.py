"""Shared pytest fixtures."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from atlas.contracts import RunContext, RunMode
from tests.fakes import (
    FakeApprovals,
    FakeClock,
    FakeContainment,
    FakeNotifier,
    FakeOutbound,
    FakeProjection,
    InMemoryStore,
)

ROOT = Path(__file__).resolve().parent
SCHEMA_DIR = ROOT / "schema"


@pytest.fixture
def schema_dir() -> Path:
    return SCHEMA_DIR


@pytest.fixture
def load_schema():
    def _load(name: str) -> dict:
        return json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))

    return _load


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def run_context() -> RunContext:
    cutoff = datetime(2026, 7, 30, 0, 0, tzinfo=timezone.utc)
    return RunContext(
        run_id="run-test",
        snapshot_id="snap-test",
        mode=RunMode.REPLAY,
        release_or_case="v1.9.0",
        cutoff=cutoff,
        alias_variant=None,
        evidence_clock=cutoff,
    )


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def outbound() -> FakeOutbound:
    return FakeOutbound()


@pytest.fixture
def approvals() -> FakeApprovals:
    return FakeApprovals()


@pytest.fixture
def projection() -> FakeProjection:
    return FakeProjection()


@pytest.fixture
def notifier() -> FakeNotifier:
    return FakeNotifier()


@pytest.fixture
def containment() -> FakeContainment:
    return FakeContainment()
