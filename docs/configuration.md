# Configuration

ATLAS reads its settings from a file named `.env` in the project folder. It then reads the
computer's environment variables. If a setting appears in both places, the environment variable
wins.

To create the file, copy `.env.example` to `.env`. Then enter your own values. Git ignores
`.env`. Never commit it.

ATLAS never writes a secret to a log. The health check at `/api/health` shows only whether each
secret is set. It never shows the value.

## Setup

```
python -m venv .venv
.venv/Scripts/python -m pip install --require-hashes -r requirements.txt      # Windows
.venv/bin/python -m pip install --require-hashes -r requirements.txt          # macOS / Linux
cp .env.example .env    # then edit .env
```

The test and audit tools are in `requirements-dev.txt`. Install that file in the same way.

## Settings

| Setting | Default | What it does |
|---|---|---|
| `ANTHROPIC_API_KEY` | empty | Key for the AI model service. Required in Connected mode. |
| `GITHUB_TOKEN` | empty | Read-only GitHub token for live data. GitHub's GraphQL interface needs a token. |
| `ATLAS_PUBLISH_TOKEN` | empty | Token that can write only to the publish target. ATLAS uses it to open draft pull requests. |
| `ATLAS_PUBLISH_TARGET` | `AnaghaP09/atlas-sandbox-target` | The only repository that ATLAS may write to. ATLAS refuses upstream project repositories. |
| `ATLAS_MODE` | `connected` | `connected` or `offline`. Offline mode refuses every outgoing connection. Steps that need the AI model then stop safely. |
| `ATLAS_SPEND_CEILING_USD` | `40` | The most ATLAS may spend on AI model calls, in US dollars. Runs stop when ATLAS reaches it. |
| `ATLAS_MODEL_REASONING` | `claude-sonnet-5-5` | AI model for Scout, Sentinel and the coding agent. |
| `ATLAS_MODEL_FAST` | `claude-haiku-5-5` | AI model for sorting, log filtering and spotting injected instructions. |
| `ATLAS_HOST` | `127.0.0.1` | The network address the server listens on. It must be a local address while the demo role switcher is in use. |
| `ATLAS_PORT` | `8000` | The port the server listens on. |
| `ATLAS_IDENTITY` | `header` | How ATLAS knows the user's role. In the demo, the `X-Atlas-Role` header carries the role. This build has no sign-in. |
| `ATLAS_UI_ORIGIN` | empty | One extra local browser address that ATLAS accepts while you work on the user interface. |
| `ATLAS_SERVER_ROLE` | `normal` | `normal` or `scenario`. The scenario server is a separate demo server with its own data. |
| `ATLAS_DEMO_SCENARIOS` | `false` | Turns on test faults and changes to the clock and the spend total. Allowed only on the scenario server. |
| `ATLAS_PEER_UI_URL` | empty | The address of the other demo server's user interface. The guided demo uses it to switch servers. |
| `ATLAS_DATA_DIR` | `data` | The folder for stores and snapshots. |
| `ATLAS_WATCH_REPOSITORY` | `kubevirt/kubevirt` | The repository that the watchers follow. |
| `ATLAS_WATCH_RELEASES` | `false` | Starts an end-of-release run when a new release tag appears. |
| `ATLAS_WATCH_FAILURES` | `false` | Starts an investigation when a new failure appears. |
| `ATLAS_FAILURE_POLL_MINUTES` | `15` | How often, in minutes, the failure watcher checks for new failures. |
| `ATLAS_NIGHTLY_TIME` | `02:00` | The local time of the nightly sweep. |
| `ATLAS_QUESTION_DIGEST` | `false` | Turns on scheduled question summaries. |
| `ATLAS_WEBHOOK_URL` | empty | Optional address for sending notifications to Slack, Mattermost or an email relay. |
| `ATLAS_WEBHOOK_AUDIENCE` | empty | Who reads that channel: a role, or `public`. If it is empty, ATLAS sends nothing to the channel. |

## Security notes

- The server checks the `Host` header of every request. It rejects the request unless the value
  is an exact local address for its port. It also rejects a browser `Origin` that is not on the
  allowed list. These checks stop a web page from reaching the local server through DNS
  rebinding. DNS rebinding is an attack that makes a browser treat a local server as part of an
  outside website.
- The dependency files list exact versions with checksums. Before every push, run
  `python -m pip_audit -r requirements.txt --require-hashes` and `scripts/public_check.py`.
