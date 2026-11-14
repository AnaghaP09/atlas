"""Start ATLAS.

    python -m atlas.serve              # the normal server; settings from .env
    python -m atlas.serve --scenario   # the scenario server; settings from .env.scenario
    python -m atlas.serve --site       # the HTTPS site with built-in sign-in (ATLAS_IDENTITY=local)

The server binds to a loopback address only while the demo role header is the identity, so it is
reachable from this machine and nowhere else. It serves the API under ``/api`` (interactive
documentation at ``/api/docs``) and, if the UI has been built into ``ui/dist``, the UI at ``/``.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import sys
from pathlib import Path

from atlas.api.__main__ import build_server
from atlas.config import ConfigError, load_settings
from atlas.housekeeping import DEFAULT_INTERVAL_SECONDS, build_housekeeper


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _site_identity(env_file: str, scenario: bool):
    """Build the HTTPS site, the session store and the sign-in provider from the settings."""
    from atlas.identity import LocalIdentity, UserDirectory
    from atlas.sessions import SessionStore
    from atlas.site import SiteConfig

    settings = load_settings(env_file)
    if settings.identity != "local":
        raise ConfigError("the HTTPS site needs ATLAS_IDENTITY=local; run `python -m deploy.init` first")
    data_dir = Path(settings.data_dir)
    users_file = data_dir / "local" / "users.toml"
    if not users_file.is_file():
        raise ConfigError(f"no users file at {users_file}; run `python -m deploy.init` first")
    site = SiteConfig(org_slug=settings.org_slug, scenario=scenario, data_dir=data_dir)
    sessions = SessionStore(site.session_db)
    provider = LocalIdentity(UserDirectory.load(users_file), sessions, default_project=settings.default_project)
    return site, sessions, provider


def _interval() -> float:
    try:
        return float(os.environ.get("ATLAS_SWEEP_SECONDS", DEFAULT_INTERVAL_SECONDS))
    except ValueError:
        return DEFAULT_INTERVAL_SECONDS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m atlas.serve", description="Start the ATLAS server.")
    parser.add_argument("--scenario", action="store_true", help="start the scenario server (.env.scenario)")
    parser.add_argument("--site", action="store_true", help="serve the HTTPS site with built-in sign-in")
    parser.add_argument("--env-file", default=None, help="settings file (default .env, or .env.scenario)")
    args = parser.parse_args(argv)
    env_file = args.env_file or (".env.scenario" if args.scenario else ".env")
    site = sessions = None
    try:
        site_identity = _site_identity(env_file, args.scenario) if args.site else None
        settings, app = build_server(env_file, args.scenario, site_identity=site_identity)
        if site_identity is not None:
            site, sessions, _ = site_identity
            options = site.uvicorn_options()
    except (ConfigError, FileNotFoundError) as exc:
        print(f"ATLAS cannot start: {exc}", file=sys.stderr)
        print("Run `python -m atlas.doctor` to check your settings.", file=sys.stderr)
        return 2
    if site is None and settings.identity == "header" and not _is_loopback(settings.host):
        print("ATLAS cannot start: with the demo role header as identity, ATLAS listens only on a loopback "
              "address (for example 127.0.0.1).", file=sys.stderr)
        return 2
    import uvicorn

    housekeeper = build_housekeeper(app.state.services, sessions, interval=_interval())
    housekeeper.start()
    try:
        if site is not None:
            print(f"ATLAS is starting at {site.origin}/  (sign in to continue)")
            uvicorn.run(app, log_level="info", **options)
        else:
            print(f"ATLAS is starting at http://{settings.host}:{settings.port}/  (API documentation: /api/docs)")
            uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")
    finally:
        housekeeper.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
