"""Start an ATLAS server.

    python -m atlas.api              # the normal server, settings from .env
    python -m atlas.api --scenario   # the scenario server, settings from .env.scenario

The scenario server refuses to start unless its settings enable demo scenarios. Both servers bind
to a loopback address only.
"""

from __future__ import annotations

import argparse
import importlib
import os
import sys
from pathlib import Path

from atlas.api.app import create_app
from atlas.api.identity import HeaderIdentity
from atlas.api.services import build_core
from atlas.projects import ProjectRegistry, default_projects_path
from atlas.config import ConfigError, load_settings, parse_env_file
from atlas.api.demo_vars import demo_variables
from atlas.contracts import Mode


def build_server(env_file: str, scenario: bool, *, site_identity: tuple | None = None):
    """Build settings and the app. ``site_identity`` is ``(site, sessions, provider)`` for the HTTPS
    site with built-in sign-in; without it the server uses the demo role header."""
    settings = load_settings(env_file)
    if scenario and not (settings.server_role == "scenario" and settings.demo_scenarios):
        raise ConfigError("the scenario server needs ATLAS_SERVER_ROLE=scenario and ATLAS_DEMO_SCENARIOS=on")
    if not scenario and settings.server_role != "normal":
        raise ConfigError("the normal server must run with ATLAS_SERVER_ROLE=normal")
    ui = frozenset({settings.ui_origin.rstrip("/")}) if settings.ui_origin else frozenset()
    services = build_core(environment="scenario" if scenario else "normal", host=settings.host, port=settings.port,
                          data_dir=Path(settings.data_dir) / "local", allowed_hosts=settings.allowed_hosts,
                          allowed_origins=settings.allowed_origins, ui_origins=ui, mode=Mode(settings.mode))
    env = parse_env_file(Path(env_file))
    env.update({k: v for k, v in os.environ.items() if k.startswith("ATLAS_")})
    services.default_project_id = env.get("ATLAS_DEFAULT_PROJECT") or None
    projects_file = env.get("ATLAS_PROJECTS_FILE") or default_projects_path(settings.data_dir)
    services.projects = ProjectRegistry.load(projects_file)
    services.projects_file = Path(projects_file)
    services.add_project_prefill = env.get("ATLAS_ADD_PROJECT", "").strip()
    services.identity = HeaderIdentity(projects=services.projects, default_project=services.default_project_id)
    services.demo_variables = demo_variables(env)
    services.config_presence = settings.presence()
    services.model_configured = bool(settings.anthropic_api_key)
    try:  # the integration module connects the data and execution layers once they exist
        wiring = importlib.import_module("atlas.wiring")
        wiring.connect(services, settings, env_file=env_file)
    except ModuleNotFoundError:
        pass
    if site_identity is None:
        return settings, create_app(services)
    site, sessions, provider = site_identity
    from atlas.identity import SessionResolver, login_router

    services.identity = SessionResolver(provider)
    return settings, create_app(services, site=site, sessions=sessions,
                                extra_routers=(login_router(provider, note="Release data comes from public "
                                                            "open-source projects."),))


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
