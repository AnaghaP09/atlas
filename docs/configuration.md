# Configuration

ATLAS reads its settings from a file named `.env` in the project folder. Environment variables
override the file. To start, copy `.env.example` to `.env` and fill in your own values.

Keep `.env` private. Git ignores it, so it is never committed. ATLAS never logs a secret and never
returns one from the API. The health check (`/api/health`) only says whether each secret is set.

Nothing in the code is tied to one machine or one person. Every value that changes between
installations is a setting. If a feature needs a setting that is empty, ATLAS turns that feature
off and tells you which setting to fill in.

## Setup

`python -m atlas.setup` writes `.env` for you and asks for the values it needs. You can also copy
`.env.example` to `.env` and edit it by hand. Then run `python -m atlas.doctor`. It checks your
settings, tools and ports, and explains any problem.

The [README](../README.md) has the full install steps.

## Secrets

| Setting | Default | What it is for |
|---|---|---|
| `ANTHROPIC_API_KEY` | empty | Key for the model service. Needed in connected mode. |
| `GITHUB_TOKEN` | empty | Read-only GitHub token for live data. Without it the live dashboard still works, but it checks GitHub every 10 minutes instead of every minute and has no edit history. Building new frozen data copies needs it. |
| `ATLAS_PUBLISH_TOKEN` | empty | Token that can write only to the publish target. ATLAS uses it to open draft pull requests. |
| `ATLAS_MODEL_API_KEY` | empty | Key for a self-hosted model server, if that server needs one. |

## Projects and publication

| Setting | Default | What it is for |
|---|---|---|
| `ATLAS_WATCH_REPOSITORY` | `<owner>/<repo>` (off) | The repository that the automatic watchers follow. |
| `ATLAS_PUBLISH_TARGET` | `<owner>/<repo>` (off) | The only repository ATLAS may open draft pull requests in. It must not be a watched repository or an upstream project. |
| `ATLAS_UPSTREAM_REPOSITORIES` | empty | Extra repositories that ATLAS reads and must never publish to, separated by commas. The watched repository and every repository in the projects file are always included. |
| `ATLAS_PROJECTS_FILE` | `<data folder>/local/projects.toml` | The file that defines your projects. Setup copies `deploy/projects.example.toml` there. A project may publish only to a target that this file grants it. |
| `ATLAS_DEFAULT_PROJECT` | empty | The project shown first when nobody has chosen one. Use an id from your projects file. |
| `ATLAS_PUBLISH_BASE_REF` | `main` | The branch of the publish target that draft pull requests start from. |
| `ATLAS_TASK_SCOPE` | `*.go` | File patterns that a coding task may change, separated by commas. |
| `ATLAS_TARGET_DIR` | `target/repo` | A local copy of the publish target's content, used to prepare changes. |
| `ATLAS_ORG_SLUG` | `demo` | Your organisation's short name. It is used in host names such as `atlas.<slug>.internal`. |

## Models

| Setting | Default | What it is for |
|---|---|---|
| `ATLAS_MODEL_PROVIDER` | `anthropic` | `anthropic` uses Claude over the internet. `openai_compatible` uses a model server inside your network, for example vLLM. |
| `ATLAS_MODEL_REASONING` | `claude-sonnet-5-5` | Model for the investigator (Scout), the verifier (Sentinel) and the coding agent. |
| `ATLAS_MODEL_FAST` | `claude-haiku-5-5` | Model for triage, log filtering and the injection check. |
| `ATLAS_MODEL_BASE_URL` | empty | Address of the self-hosted model server. Needed for `openai_compatible`. |
| `ATLAS_MODEL_INTERNAL_HOSTS` | empty | Host names of model servers inside your network, separated by commas. In offline mode, the model server must be in this list. |

## Mode and spending

| Setting | Default | What it is for |
|---|---|---|
| `ATLAS_MODE` | `connected` | `connected` or `offline`. In offline mode, every connection outside your network is refused, and model steps stop unless a self-hosted model is set up. |
| `ATLAS_SPEND_CEILING_USD` | `40` | The most ATLAS may spend on model calls. Runs stop when this is reached. |
| `ATLAS_INJECTION_CLASSIFIER` | `on` | A small model judges text that looks like an instruction to an AI. `off` uses fixed patterns only, which flag much more ordinary text. |

## Server and sign-in

| Setting | Default | What it is for |
|---|---|---|
| `ATLAS_HOST` | `127.0.0.1` | The address the server listens on. |
| `ATLAS_PORT` | `8000` | The port the server listens on. |
| `ATLAS_IDENTITY` | `header` | How people are identified. `header` reads a role header and is for tests and development only; the server then accepts only local connections. `local` is the built-in sign-in. `oidc` (company single sign-on) is not available yet. |
| `ATLAS_UI_ORIGIN` | empty | An extra local browser address allowed while you develop the UI. The example file sets `http://127.0.0.1:5173`. |
| `ATLAS_SERVER_ROLE` | `normal` | `normal`, or `scenario` for a separate demo server with its own data. |
| `ATLAS_DEMO_SCENARIOS` | `false` | Turns on demo faults and clock and spend changes. Allowed only on the scenario server. |
| `ATLAS_PEER_UI_URL` | empty | The address of the other demo server's UI, for the guided demo. The example file sets `http://127.0.0.1:8001`. |
| `ATLAS_SWEEP_SECONDS` | `300` | How often the server checks for expired approvals, access grants and sessions. |

## Data, sandbox and tools

| Setting | Default | What it is for |
|---|---|---|
| `ATLAS_DATA_DIR` | `data` | Where ATLAS keeps its stores and snapshots. A relative path is relative to the project folder. |
| `ATLAS_IMAGE_CACHE` | `data/local/images` | Where the sandbox container images are kept after the one-time download. |
| `ATLAS_SANDBOX_RUNNER` | `local_docker` | Where code checks run. `local_docker` uses a locked-down container on this machine. `remote` hands them to a separate runner (not available yet). `none` never runs project code, so a task can be prepared but never approved for publication. |
| `ATLAS_DOCKER_BIN` | empty | Full path to Docker. Leave empty to find it on `PATH`. |
| `ATLAS_GO_BIN` | empty | Full path to Go. Leave empty to find it on `PATH`. |
| `ATLAS_OPENSSL_BIN` | empty | Full path to OpenSSL. Leave empty to find it on `PATH`. |

## Automatic runs

| Setting | Default | What it is for |
|---|---|---|
| `ATLAS_WATCH_RELEASES` | `false` | Start a release-close run when a new release tag appears. |
| `ATLAS_WATCH_FAILURES` | `false` | Start an investigation when a new failure appears. |
| `ATLAS_FAILURE_POLL_MINUTES` | `15` | How often the failure watcher checks for new failures. |
| `ATLAS_NIGHTLY_TIME` | `02:00` | Local time of the nightly sweep. |
| `ATLAS_QUESTION_DIGEST` | `false` | Scheduled question digests. |
| `ATLAS_DASHBOARD_POLL` | `on` | `off` stops the live dashboard from checking GitHub. |
| `ATLAS_DASHBOARD_POLL_SECONDS` | `60` with a GitHub token, `600` without | How often the live dashboard checks each project. Offline mode pauses it. |

## Demo script

The guided demo and `demo/run_demo.py` fill these values into their steps. Leave one empty to
skip the steps that need it.

| Setting | Default | What it is for |
|---|---|---|
| `ATLAS_BASE_URL` | empty | Address of the main server. Empty means it is built from `ATLAS_HOST` and `ATLAS_PORT`. |
| `ATLAS_SCENARIO_URL` | empty | Address of the scenario server. Empty means the next port. |
| `ATLAS_DEMO_RELEASE_SNAPSHOT` | empty | Id of the frozen data copy for the release-close step. |
| `ATLAS_DEMO_ALIASED_SNAPSHOT` | empty | Id of its test copy, which has names disguised. |
| `ATLAS_DEMO_RELEASE_ID` | empty | The release that the step closes, for example `v1.9.0`. |
| `ATLAS_DEMO_RELEASE_FILTER` | empty | A filter for the release list. |
| `ATLAS_DEMO_CASE_G01`, `_G03`, `_G04` | empty | Ids of three example cases used by the investigation steps. |
| `ATLAS_DEMO_PASSWORD_<ROLE>` | not in `.env` | The demo user's password for a role, for example `ATLAS_DEMO_PASSWORD_EM`. Set it as an environment variable when the demo script signs in. Never put it in a file. |

## Notifications

| Setting | Default | What it is for |
|---|---|---|
| `ATLAS_WEBHOOK_URL` | empty | Optional webhook for Slack, Mattermost or an email relay. |
| `ATLAS_WEBHOOK_AUDIENCE` | empty | Who reads that channel: a role or `public`. ATLAS filters content for that audience. If no audience is set, nothing is sent. |

## Security notes

- The server rejects a request if its `Host` header is not on the exact allow-list for its port.
  It also rejects a browser `Origin` that is not on the allow-list. This stops web pages from
  reaching the local server through DNS rebinding.
- Dependencies are locked with hashes. Before every push, run
  `python -m pip_audit -r requirements.txt --require-hashes` and `python scripts/public_check.py --history`.
