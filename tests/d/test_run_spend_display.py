"""A run shows its own model spend only when that was recorded for the run itself."""

from atlas.api.routes_runs import _run_cost


def test_spend_recorded_for_runs_that_counted_their_own_share():
    out = _run_cost({"steps": 3, "tool_calls": 4, "seconds": 9.0, "usd": 0.12, "input_tokens": 900,
                     "output_tokens": 80, "per_run": True})
    assert out["spend_recorded"] is True
    assert out["usd"] == 0.12 and out["input_tokens"] == 900
    assert "per_run" not in out


def test_older_runs_show_spend_as_not_recorded():
    out = _run_cost({"steps": 3, "tool_calls": 4, "seconds": 9.0, "usd": 0.676, "input_tokens": 5000,
                     "output_tokens": 300})
    assert out["spend_recorded"] is False
    assert out["usd"] is None and out["input_tokens"] is None and out["output_tokens"] is None
    assert out["steps"] == 3  # step and tool counts were always per run


def test_missing_cost_stays_missing():
    assert _run_cost(None) is None
