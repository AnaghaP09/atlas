"""Server for the browser tests: the real application with recorded model answers.

It is built the same way ``python -m atlas.serve`` builds it. Three things differ: the model
provider returns recorded answers and never reaches a real model, the data folder is a fresh
temporary copy of a small part of the reference data, and live polling is off.

Usage: python tests/e2e/server.py --port 8791
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from atlas.contracts import ModelResult  # noqa: E402

DATA_PARTS = ("architecture", "releases/release-close-v1.9.0", "cases/case-g01", "synthetic")


class RecordedProvider:
    """Recorded answers per model role. The verifier answer supports every claim it is shown."""

    def complete(self, role, messages, schema, limits):
        text = " ".join(str(m.get("content", "")) for m in messages)
        if role == "verifier":
            ids = list(dict.fromkeys(re.findall(r'"claim_id":\s*"([^"]+)"', text)))
            verdicts = [{"claim_id": i, "verdict": "supported", "reason": "the cited record states this",
                         "contradicting_record": None} for i in ids]
            return ModelResult({"verdicts": verdicts}, "complete", 10, 10, "recorded", "recorded")
        if role == "injection_classifier":
            return ModelResult({"injection": False, "span": "", "reason": "ordinary engineering text"},
                               "complete", 5, 5, "recorded", "recorded")
        return ModelResult({}, "failed", 0, 0, "recorded", "recorded")


def prepare_data(port: int) -> Path:
    data = Path(tempfile.mkdtemp(prefix="atlas-e2e-"))
    for part in DATA_PARTS:
        src = ROOT / "data" / "snapshots" / part
        if src.exists():
            shutil.copytree(src, data / "snapshots" / part)
    if (ROOT / "data" / "evaluator").exists():
        shutil.copytree(ROOT / "data" / "evaluator", data / "evaluator")
    (data / "local").mkdir(parents=True, exist_ok=True)
    projects = (ROOT / "deploy" / "projects.example.toml").read_text(encoding="utf-8")
    projects = projects.replace("publish_targets = []", 'publish_targets = ["example/target"]', 1)
    (data / "local" / "projects.toml").write_text(projects, encoding="utf-8")
    env = [
        f"ATLAS_DATA_DIR={data.as_posix()}", "ATLAS_HOST=127.0.0.1", f"ATLAS_PORT={port}",
        "ATLAS_DEFAULT_PROJECT=kubevirt", "ATLAS_PUBLISH_TARGET=example/target", "ATLAS_SANDBOX_RUNNER=none",
        "ATLAS_DASHBOARD_POLL=off", "ATLAS_SPEND_CEILING_USD=5", "ATLAS_IDENTITY=header",
        "ATLAS_DEMO_RELEASE_SNAPSHOT=release-close-v1.9.0", "ATLAS_DEMO_RELEASE_ID=v1.9.0",
        "ATLAS_DEMO_RELEASE_FILTER=v1.9.0", "ATLAS_DEMO_CASE_G01=case-g01",
    ]
    (data / ".env").write_text("\n".join(env) + "\n", encoding="utf-8")
    return data


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8791)
    args = parser.parse_args()
    for name in [k for k in os.environ if k.startswith("ATLAS_") or k in ("ANTHROPIC_API_KEY", "GITHUB_TOKEN")]:
        os.environ.pop(name)
    data = prepare_data(args.port)

    import atlas.wiring as wiring

    wiring.provider_from_env = lambda env: RecordedProvider()

    from atlas.api.__main__ import build_server

    settings, app = build_server(str(data / ".env"), False)
    app.state.services.model_configured = True

    import uvicorn

    try:
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    finally:
        shutil.rmtree(data, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
