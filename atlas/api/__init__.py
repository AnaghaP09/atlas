"""HTTP API for ATLAS. Every route lives under ``/api``; see ``schema/api.md``."""

from atlas.api.app import create_app

__all__ = ["create_app"]
