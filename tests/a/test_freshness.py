from datetime import timedelta

import pytest

from atlas.freshness import MAX_AGE, FreshnessStatus, evaluate, fresh_until, reconstructed_confirmation
from tests.a.helpers import T0

AGEING = [s for s, limit in MAX_AGE.items() if limit is not None]


@pytest.mark.parametrize("source", AGEING)
def test_one_second_inside_and_outside_each_limit(source):
    limit = MAX_AGE[source]
    clock = T0
    inside = evaluate(source, clock - limit + timedelta(seconds=1), clock)
    outside = evaluate(source, clock - limit - timedelta(seconds=1), clock)
    assert inside.status is FreshnessStatus.FRESH
    assert outside.status is FreshnessStatus.STALE
    # fresh_until and the age check always agree
    assert inside.fresh_until > clock
    assert outside.fresh_until < clock


def test_missing_confirmation_is_unknown():
    assert evaluate("issues", None, T0).status is FreshnessStatus.UNKNOWN


def test_future_confirmation_is_unknown_never_fresh():
    result = evaluate("security", T0 + timedelta(minutes=1), T0)
    assert result.status is FreshnessStatus.UNKNOWN
    assert not result.usable


def test_unchanged_old_issue_is_fresh_when_confirmed_recently():
    # Last change 30 days ago is irrelevant: freshness comes from confirmation.
    confirmed = T0 - timedelta(minutes=5)
    result = evaluate("issues", confirmed, T0)
    assert result.status is FreshnessStatus.FRESH
    assert fresh_until("issues", confirmed) > T0


def test_reconstructed_history_complete_through_cutoff_confirms_cutoff():
    assert reconstructed_confirmation(T0, T0 + timedelta(days=100)) == T0
    assert evaluate("issues", reconstructed_confirmation(T0, T0 + timedelta(days=1)), T0).status is FreshnessStatus.FRESH


def test_reconstructed_history_incomplete_is_unknown():
    assert reconstructed_confirmation(T0, None) is None
    assert reconstructed_confirmation(T0, T0 - timedelta(seconds=1)) is None
    assert evaluate("issues", reconstructed_confirmation(T0, None), T0).status is FreshnessStatus.UNKNOWN


def test_identity_bound_sources_do_not_age():
    assert evaluate("git", T0 - timedelta(days=400), T0).status is FreshnessStatus.FRESH
    assert fresh_until("git", T0) is None


def test_scan_expires_between_gate_and_later_check():
    scan_time = T0
    at_gate = evaluate("security", scan_time, scan_time + timedelta(hours=23))
    later = evaluate("security", scan_time, scan_time + timedelta(hours=25))
    assert at_gate.usable and not later.usable
    assert at_gate.fresh_until == scan_time + timedelta(hours=24)


def test_naive_timestamps_are_unknown():
    assert evaluate("issues", T0.replace(tzinfo=None), T0).status is FreshnessStatus.UNKNOWN
