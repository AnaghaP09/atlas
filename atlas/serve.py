"""Start ATLAS.

    python -m atlas.serve              # the normal server; settings from .env
    python -m atlas.serve --scenario   # the scenario server; settings from .env.scenario

The server binds to a loopback address only while the demo role header is the identity, so it is
reachable from this machine and nowhere else. It serves the API under ``/api`` (interactive
documentation at ``/api/docs``) and, if the UI has been built into ``ui/dist``, the UI at ``/``.
"""

from __future__ import annotations

import argparse
import ipaddress
import sys

from atlas.api.__main__ import build_server
from atlas.config import ConfigError


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m atlas.serve", description="Start the ATLAS server.")
    parser.add_argument("--scenario", action="store_true", help="start the scenario server (.env.scenario)")
    parser.add_argument("--env-file", default=None, help="settings file (default .env, or .env.scenario)")
    args = parser.parse_args(argv)
    env_file = args.env_file or (".env.scenario" if args.scenario else ".env")
    try:
        settings, app = build_server(env_file, args.scenario)
    except ConfigError as exc:
        print(f"ATLAS cannot start: {exc}", file=sys.stderr)
        print("Run `python -m atlas.doctor` to check your settings.", file=sys.stderr)
        return 2
    if settings.identity == "header" and not _is_loopback(settings.host):
        print("ATLAS cannot start: with the demo role header as identity, ATLAS listens only on a loopback "
              "address (for example 127.0.0.1).", file=sys.stderr)
        return 2
    import uvicorn

    print(f"ATLAS is starting at http://{settings.host}:{settings.port}/  (API documentation: /api/docs)")
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
