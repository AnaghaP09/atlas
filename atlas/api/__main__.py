"""Start an ATLAS server.

    python -m atlas.api              # the normal server, settings from .env
    python -m atlas.api --scenario   # the scenario server, settings from .env.scenario

The scenario server refuses to start unless its settings enable demo scenarios. Both servers bind
to a loopback address only.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

from atlas.api.app import create_app
from atlas.api.services import build_core
from atlas.config import ConfigError, load_settings
from atlas.contracts import Mode


def build_server(env_file: str, scenario: bool):
    settings = load_settings(env_file)
    if scenario and not (settings.server_role == "scenario" and settings.demo_scenarios):
        raise ConfigError("the scenario server needs ATLAS_SERVER_ROLE=scenario and ATLAS_DEMO_SCENARIOS=on")
    if not scenario and settings.server_role != "normal":
        raise ConfigError("the normal server must run with ATLAS_SERVER_ROLE=normal")
    extra = frozenset({settings.ui_origin.rstrip("/")}) if settings.ui_origin else frozenset()
    services = build_core(environment="scenario" if scenario else "normal", port=settings.port,
                          data_dir=Path(settings.data_dir) / "local", extra_origins=extra, mode=Mode(settings.mode))
    services.config_presence = settings.presence()
    services.model_configured = bool(settings.anthropic_api_key)
    try:  # the integration module connects the data and execution layers once they exist
        wiring = importlib.import_module("atlas.wiring")
        wiring.connect(services, settings)
    except ModuleNotFoundError:
        pass
    return settings, create_app(services)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m atlas.api")
    parser.add_argument("--scenario", action="store_true", help="start the scenario server")
    args = parser.parse_args(argv)
    try:
        settings, app = build_server(".env.scenario" if args.scenario else ".env", args.scenario)
    except ConfigError as exc:
        print(f"ATLAS cannot start: {exc}", file=sys.stderr)
        return 2
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
