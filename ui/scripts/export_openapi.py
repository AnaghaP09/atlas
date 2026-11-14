"""Print the backend's OpenAPI document. Run from the repository root's environment.

    python ui/scripts/export_openapi.py > ui/openapi.json
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from atlas.api.app import create_app  # noqa: E402
from atlas.api.services import build_core  # noqa: E402

spec = create_app(build_core()).openapi()
sys.stdout.write(json.dumps(spec, indent=2, sort_keys=True) + "\n")
