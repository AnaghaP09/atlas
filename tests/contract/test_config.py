"""Configuration loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from atlas.config import ConfigError, Secret, load_settings, parse_env_file


def test_defaults_without_any_file() -> None:
    s = load_settings(env_file=None, environ={})
    assert s.host == "127.0.0.1" and s.port == 8000
    assert s.mode == "connected" and s.spend_ceiling_usd == 40
    assert s.model_reasoning == "claude-sonnet-5-5"


def test_env_file_then_environment_override(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("ATLAS_PORT=8100\nATLAS_MODE=offline\n# comment\n", encoding="utf-8")
    s = load_settings(env_file=env, environ={"ATLAS_PORT": "8200"})
    assert s.port == 8200 and s.mode == "offline"


def test_parse_strips_quotes_and_bom(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text('﻿A="x"\nB=\'y\'\nC=z=1\n', encoding="utf-8")
    assert parse_env_file(env) == {"A": "x", "B": "y", "C": "z=1"}


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "example.com"])
def test_non_loopback_bind_refused_with_header_identity(host: str) -> None:
    with pytest.raises(ConfigError, match="loopback"):
        load_settings(env_file=None, environ={"ATLAS_HOST": host})


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_bind_allowed(host: str) -> None:
    assert load_settings(env_file=None, environ={"ATLAS_HOST": host}).host == host


def test_host_and_origin_allow_lists() -> None:
    s = load_settings(env_file=None, environ={"ATLAS_PORT": "8001", "ATLAS_UI_ORIGIN": "http://127.0.0.1:5173"})
    assert s.allowed_hosts == {"127.0.0.1:8001", "localhost:8001", "[::1]:8001"}
    assert "http://127.0.0.1:5173" in s.allowed_origins
    assert "http://attacker.example:8001" not in s.allowed_origins


def test_ui_origin_must_be_loopback() -> None:
    with pytest.raises(ConfigError, match="ATLAS_UI_ORIGIN"):
        load_settings(env_file=None, environ={"ATLAS_UI_ORIGIN": "https://attacker.example"})


def test_upstream_publish_target_refused() -> None:
    with pytest.raises(ConfigError, match="upstream"):
        load_settings(env_file=None, environ={"ATLAS_PUBLISH_TARGET": "kubevirt/kubevirt"})


def test_scenarios_only_on_scenario_server() -> None:
    with pytest.raises(ConfigError, match="scenario"):
        load_settings(env_file=None, environ={"ATLAS_DEMO_SCENARIOS": "true"})
    s = load_settings(env_file=None, environ={"ATLAS_DEMO_SCENARIOS": "true", "ATLAS_SERVER_ROLE": "scenario"})
    assert s.demo_scenarios


def test_model_key_required_only_when_asked_and_connected() -> None:
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        load_settings(env_file=None, environ={}, require_model_key=True)
    load_settings(env_file=None, environ={"ATLAS_MODE": "offline"}, require_model_key=True)


def test_invalid_values_reported_together() -> None:
    with pytest.raises(ConfigError) as err:
        load_settings(env_file=None, environ={"ATLAS_MODE": "x", "ATLAS_PORT": "abc", "ATLAS_SPEND_CEILING_USD": "-1"})
    message = str(err.value)
    assert "ATLAS_MODE" in message and "ATLAS_PORT" in message and "ATLAS_SPEND_CEILING_USD" in message


def test_secrets_never_printed() -> None:
    value = "sk-ant-" + "x" * 40
    s = load_settings(env_file=None, environ={"ANTHROPIC_API_KEY": value})
    assert value not in repr(s) and value not in str(s)
    assert s.anthropic_api_key.reveal() == value
    assert s.presence()["ANTHROPIC_API_KEY"] is True
    assert repr(Secret("")) == "Secret(unset)"


def test_error_message_never_contains_secret() -> None:
    value = "sk-ant-" + "y" * 40
    with pytest.raises(ConfigError) as err:
        load_settings(env_file=None, environ={"ANTHROPIC_API_KEY": value, "ATLAS_HOST": "0.0.0.0"})
    assert value not in str(err.value)


def test_webhook_without_audience_warns() -> None:
    s = load_settings(env_file=None, environ={"ATLAS_WEBHOOK_URL": "http://127.0.0.1:9999/hook"})
    assert any("AUDIENCE" in w for w in s.warnings)
    with pytest.raises(ConfigError):
        load_settings(env_file=None, environ={"ATLAS_WEBHOOK_AUDIENCE": "everyone"})


def test_example_file_loads(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    s = load_settings(env_file=root / ".env.example", environ={})
    assert s.publish_target == "AnaghaP09/atlas-sandbox-target"
    assert not s.anthropic_api_key
