# Configuration

ATLAS reads settings from a `.env` file in the project root, then from the environment
(environment variables win). Copy `.env.example` to `.env` and fill in your values. `.env` is
git-ignored; never commit it. Secrets are never logged or returned by the API; `/api/health`
reports only whether each one is set.

## Setup

```
python -m venv .venv
.venv/Scripts/python -m pip install --require-hashes -r requirements.txt      # Windows
.venv/bin/python -m pip install --require-hashes -r requirements.txt          # macOS / Linux
cp .env.example .env    # then edit .env
```

Development tools (tests, dependency audit): install `requirements-dev.txt` the same way.

## Settings

| Key | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | empty | Model API key. Required in connected mode. |
| `GITHUB_TOKEN` | empty | Read-only token for live data fetches from GitHub (GraphQL needs authentication). |
| `ATLAS_PUBLISH_TOKEN` | empty | Token scoped to the publish target only, used to open draft pull requests. |
| `ATLAS_PUBLISH_TARGET` | `AnaghaP09/atlas-sandbox-target` | The only repository ATLAS may publish to. Upstream project repositories are refused. |
| `ATLAS_MODE` | `connected` | `connected` or `offline`. Offline refuses every outbound connection; model steps fail closed. |
| `ATLAS_SPEND_CEILING_USD` | `40` | Hard ceiling on model spend; runs stop when reached. |
| `ATLAS_MODEL_REASONING` | `claude-sonnet-5-5` | Model for the investigator, verifier and coding agent. |
| `ATLAS_MODEL_FAST` | `claude-haiku-5-5` | Model for triage, log filtering and the injection classifier. |
| `ATLAS_HOST` | `127.0.0.1` | Bind address. Must be loopback while the demo role header is the identity. |
| `ATLAS_PORT` | `8000` | Port. |
| `ATLAS_IDENTITY` | `header` | Demo identity via the `X-Atlas-Role` header. Sign-in is not part of this build. |
| `ATLAS_UI_ORIGIN` | empty | Extra loopback browser origin allowed during UI development. |
| `ATLAS_SERVER_ROLE` | `normal` | `normal` or `scenario` (a separate demo server with its own data). |
| `ATLAS_DEMO_SCENARIOS` | `false` | Enables fault, clock and spend injection. Allowed only on the scenario server. |
| `ATLAS_PEER_UI_URL` | empty | The other demo server's UI, for guided-demo hand-off. |
| `ATLAS_DATA_DIR` | `data` | Where stores and snapshots are kept. |
| `ATLAS_WATCH_REPOSITORY` | `kubevirt/kubevirt` | Repository followed by the watchers. |
| `ATLAS_WATCH_RELEASES` | `false` | Start a release-close run when a new release tag appears. |
| `ATLAS_WATCH_FAILURES` | `false` | Start investigations for new failure events. |
| `ATLAS_FAILURE_POLL_MINUTES` | `15` | Failure watcher polling interval. |
| `ATLAS_NIGHTLY_TIME` | `02:00` | Local time of the nightly sweep. |
| `ATLAS_QUESTION_DIGEST` | `false` | Scheduled question digests. |
| `ATLAS_WEBHOOK_URL` | empty | Optional Slack, Mattermost or email-relay webhook. |
| `ATLAS_WEBHOOK_AUDIENCE` | empty | Who reads that channel: a role or `public`. Without it nothing is sent. |

## Security notes

- The server rejects any request whose `Host` header is not an exact loopback value for its port,
  and any browser `Origin` outside the allow-list. This blocks pages that try to reach the local
  server through DNS rebinding.
- Dependencies are locked with hashes. Run `python -m pip_audit -r requirements.txt
  --require-hashes` and `scripts/public_check.py` before every push.
