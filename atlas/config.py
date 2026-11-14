"""Configuration loaded from a ``.env`` file and the environment.

Real environment variables override values in ``.env``. Secrets are wrapped so they never appear
in logs, reprs or the health endpoint; only their presence is reported.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
UPSTREAM_REPOSITORIES = {"kubevirt/kubevirt"}


class ConfigError(ValueError):
    """Raised when the configuration is invalid; the message never contains secret values."""


class Secret:
    """A string that never reveals its value when printed."""

    __slots__ = ("_value",)

    def __init__(self, value: str | None) -> None:
        self._value = value or ""

    def reveal(self) -> str:
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __repr__(self) -> str:
        return "Secret(set)" if self._value else "Secret(unset)"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and other._value == self._value

    def __hash__(self) -> int:
        return hash(("secret", bool(self._value)))


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines. Blank lines and lines starting with # are ignored."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _is_loopback(host: str) -> bool:
    if host in LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: Secret
    github_token: Secret
    publish_token: Secret
    publish_target: str
    mode: str
    spend_ceiling_usd: float
    host: str
    port: int
    identity: str
    ui_origin: str
    server_role: str
    demo_scenarios: bool
    peer_ui_url: str
    data_dir: Path
    model_reasoning: str
    model_fast: str
    webhook_url: Secret
    webhook_audience: str
    watch_repository: str
    watch_releases: bool
    watch_failures: bool
    failure_poll_minutes: int
    nightly_time: str
    question_digest: bool
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def allowed_hosts(self) -> frozenset[str]:
        """Exact Host header values accepted by the server."""
        return frozenset(
            {f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"[::1]:{self.port}"}
        )

    @property
    def allowed_origins(self) -> frozenset[str]:
        """Exact Origin values accepted from browsers."""
        origins = {f"http://{h}" for h in self.allowed_hosts}
        if self.ui_origin:
            origins.add(self.ui_origin.rstrip("/"))
        return frozenset(origins)

    def presence(self) -> dict[str, bool]:
        """Which secrets are configured, for the health endpoint. Never the values."""
        return {
            "ANTHROPIC_API_KEY": bool(self.anthropic_api_key),
            "GITHUB_TOKEN": bool(self.github_token),
            "ATLAS_PUBLISH_TOKEN": bool(self.publish_token),
            "ATLAS_WEBHOOK_URL": bool(self.webhook_url),
        }


def load_settings(
    env_file: Path | str | None = ".env",
    environ: Mapping[str, str] | None = None,
    *,
    require_model_key: bool = False,
) -> Settings:
    """Load and validate settings. Raises ConfigError with every problem found."""
    file_values = parse_env_file(Path(env_file)) if env_file else {}
    env = dict(file_values)
    env.update({k: v for k, v in (os.environ if environ is None else environ).items() if k.startswith(("ATLAS_", "ANTHROPIC_", "GITHUB_"))})

    def get(key: str, default: str = "") -> str:
        value = env.get(key, "")
        return value if value != "" else default

    errors: list[str] = []
    warnings: list[str] = []

    mode = get("ATLAS_MODE", "connected").lower()
    if mode not in {"connected", "offline"}:
        errors.append("ATLAS_MODE must be 'connected' or 'offline'")

    try:
        ceiling = float(get("ATLAS_SPEND_CEILING_USD", "40"))
        if ceiling <= 0:
            errors.append("ATLAS_SPEND_CEILING_USD must be greater than 0")
    except ValueError:
        ceiling = 0.0
        errors.append("ATLAS_SPEND_CEILING_USD must be a number")

    try:
        port = int(get("ATLAS_PORT", "8000"))
        if not 1 <= port <= 65535:
            errors.append("ATLAS_PORT must be between 1 and 65535")
    except ValueError:
        port = 0
        errors.append("ATLAS_PORT must be an integer")

    host = get("ATLAS_HOST", "127.0.0.1")
    identity = get("ATLAS_IDENTITY", "header").lower()
    if identity not in {"header"}:
        errors.append("ATLAS_IDENTITY must be 'header' (sign-in is not available in this build)")
    if identity == "header" and not _is_loopback(host):
        errors.append(
            "ATLAS_HOST must be a loopback address (127.0.0.1 or ::1) while the demo role header "
            "is the identity; exposing the server needs an authenticated identity provider"
        )

    server_role = get("ATLAS_SERVER_ROLE", "normal").lower()
    if server_role not in {"normal", "scenario"}:
        errors.append("ATLAS_SERVER_ROLE must be 'normal' or 'scenario'")
    demo_scenarios = _bool(env.get("ATLAS_DEMO_SCENARIOS"))
    if demo_scenarios and server_role != "scenario":
        errors.append("ATLAS_DEMO_SCENARIOS may only be enabled on the scenario server")

    publish_target = get("ATLAS_PUBLISH_TARGET")
    if publish_target and publish_target.lower() in UPSTREAM_REPOSITORIES:
        errors.append("ATLAS_PUBLISH_TARGET must not be an upstream project repository")
    if publish_target and publish_target.count("/") != 1:
        errors.append("ATLAS_PUBLISH_TARGET must be 'owner/repository'")

    ui_origin = get("ATLAS_UI_ORIGIN")
    if ui_origin:
        origin_host = ui_origin.split("://", 1)[-1].rsplit(":", 1)[0]
        if not _is_loopback(origin_host):
            errors.append("ATLAS_UI_ORIGIN must be a loopback origin")

    webhook_url = get("ATLAS_WEBHOOK_URL")
    webhook_audience = get("ATLAS_WEBHOOK_AUDIENCE")
    if webhook_url and not webhook_audience:
        warnings.append(
            "ATLAS_WEBHOOK_URL has no ATLAS_WEBHOOK_AUDIENCE; deliveries will be held until an audience is declared"
        )
    if webhook_audience and webhook_audience not in {"pm", "em", "architect", "security", "oncall", "audit", "public"}:
        errors.append("ATLAS_WEBHOOK_AUDIENCE must be a role or 'public'")

    try:
        poll = int(get("ATLAS_FAILURE_POLL_MINUTES", "15"))
        if poll < 1:
            errors.append("ATLAS_FAILURE_POLL_MINUTES must be at least 1")
    except ValueError:
        poll = 0
        errors.append("ATLAS_FAILURE_POLL_MINUTES must be an integer")

    nightly = get("ATLAS_NIGHTLY_TIME", "02:00")
    parts = nightly.split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts) or not (0 <= int(parts[0]) < 24 and 0 <= int(parts[1]) < 60):
        errors.append("ATLAS_NIGHTLY_TIME must be HH:MM")

    api_key = Secret(get("ANTHROPIC_API_KEY"))
    if require_model_key and mode == "connected" and not api_key:
        errors.append("ANTHROPIC_API_KEY is required in connected mode")

    if errors:
        raise ConfigError("; ".join(errors))

    return Settings(
        anthropic_api_key=api_key,
        github_token=Secret(get("GITHUB_TOKEN")),
        publish_token=Secret(get("ATLAS_PUBLISH_TOKEN")),
        publish_target=publish_target,
        mode=mode,
        spend_ceiling_usd=ceiling,
        host=host,
        port=port,
        identity=identity,
        ui_origin=ui_origin,
        server_role=server_role,
        demo_scenarios=demo_scenarios,
        peer_ui_url=get("ATLAS_PEER_UI_URL"),
        data_dir=Path(get("ATLAS_DATA_DIR", "data")),
        model_reasoning=get("ATLAS_MODEL_REASONING", "claude-sonnet-5-5"),
        model_fast=get("ATLAS_MODEL_FAST", "claude-haiku-5-5"),
        webhook_url=Secret(webhook_url),
        webhook_audience=webhook_audience,
        watch_repository=get("ATLAS_WATCH_REPOSITORY", "kubevirt/kubevirt"),
        watch_releases=_bool(env.get("ATLAS_WATCH_RELEASES"), default=False),
        watch_failures=_bool(env.get("ATLAS_WATCH_FAILURES"), default=False),
        failure_poll_minutes=poll,
        nightly_time=nightly,
        question_digest=_bool(env.get("ATLAS_QUESTION_DIGEST"), default=False),
        warnings=tuple(warnings),
    )
