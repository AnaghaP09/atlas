"""Demo values come from configuration, never from code.

The demo manifest uses placeholders such as ``{release_snapshot}``. Its ``variables`` section maps
each placeholder to a setting name (for example ``ATLAS_DEMO_RELEASE_SNAPSHOT``). The server and the
command-line runner fill the placeholders from the environment or ``.env``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

MANIFEST = Path(__file__).resolve().parents[2] / "demo" / "manifest.json"


def variable_settings(manifest_path: Path = MANIFEST) -> dict[str, str]:
    return dict(json.loads(manifest_path.read_text(encoding="utf-8")).get("variables", {}))


def demo_variables(env: Mapping[str, str], manifest_path: Path = MANIFEST) -> dict[str, str]:
    """Placeholder values found in ``env``; placeholders without a setting stay unfilled."""
    return {name: env[key] for name, key in variable_settings(manifest_path).items() if env.get(key)}


def missing_variables(env: Mapping[str, str], manifest_path: Path = MANIFEST) -> list[str]:
    return sorted(key for name, key in variable_settings(manifest_path).items() if not env.get(key))
