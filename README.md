# ATLAS

ATLAS is an engineering control plane for teams that ship software on release trains. At the
close of each release it forecasts which technical debt and open issues the next release will
inherit, proposes who should take each item (a developer, a sandboxed coding agent, or a human
decision), and investigates failures that happen mid-release, citing the evidence behind every
claim it makes.

ATLAS recommends; people decide. It never sets scope, staffing or dates, never writes outside a
sandbox branch, and never states anything without a citation.

## Who it is for

| Reader | Report ATLAS gives them | Decision it supports |
|---|---|---|
| Product manager | Next-release debt and risk forecast | Which debt enters the release, the debt budget, scope cuts and the date |
| Engineering manager | Allocation plan across developers and coding agents; investigation assessments | What to delegate, what to assign, where to add people |
| Solution architect | Architecture hotspots and debt-acceptance report | Which debt to accept with a recorded reason, and which to fix |

Security engineers, on-call engineers and component owners also receive security-gate reports,
investigation hand-offs and notifications.

## How it works

1. **Triggers.** A run starts automatically (a new release tag, a new failure, a nightly sweep)
   or manually from the UI. Both paths share the same guardrails, budgets and audit record.
2. **Evidence.** Connectors read the issue tracker, git, CI, a security scanner, an architecture
   graph and, in the demo, synthetic cluster, telemetry and capacity data. Every run reads one
   frozen snapshot, so results can be reproduced.
3. **Two agents.** An *investigator* decides what to read next from what it has just read. A
   *verifier* checks every claim against the records it cites and never sees the investigator's
   reasoning. Anything resting on an unsupported claim is removed before a report is published.
4. **Calculation services.** Forecasting, allocation and the security gate are ordinary code,
   not agents.
5. **Approvals and the coding agent.** A task delegated to a coding agent needs two
   engineering-manager approvals: one to prepare the change, and one on the exact diff. The
   agent works in a container with no network access and no secrets. The result is opened as a
   draft pull request on a separate sandbox repository,
   [atlas-sandbox-target](https://github.com/AnaghaP09/atlas-sandbox-target), and is never
   merged automatically.
6. **Notifications.** Every error, approval request and safety event creates a notification,
   routed by role, shown in the UI and optionally sent to a chat webhook.

More detail: [architecture](docs/architecture.md), [data sources](docs/data-sources.md),
[configuration](docs/configuration.md), [deployment](docs/deployment.md),
[security](SECURITY.md).

## Status

ATLAS is being built. The shared data formats, interfaces and configuration are in place; the
connectors, agents, execution path, HTTP API and web UI are in progress. This README will say
when each part works. Until then, treat everything described here as the design.

The demonstration labels every result with where its data came from:

| Label | Meaning |
|---|---|
| `REAL` | Real public data and real model calls |
| `SYNTHETIC` | Data the team built, marked as such and never scored as observed |
| `STAGED` | The code path exists, but the real-world piece is outside this build (for example a self-hosted model for offline mode) |
| `UNEVALUATED` | A quality bar this build cannot measure |

ATLAS is domain-agnostic. Its reference ecosystem for the demo and the tests is the public
[KubeVirt](https://github.com/kubevirt/kubevirt) project. KubeVirt does not record which issues
blocked each release, so forecast accuracy is measured against a stated proxy definition of a
release blocker (see [data sources](docs/data-sources.md)), and every result says so.

## Quick start

Requirements: Python 3.14, Node.js 24 and Docker (Docker runs the coding agent's sandbox).

```
git clone https://github.com/AnaghaP09/atlas.git
cd atlas
python -m venv .venv
.venv/Scripts/python -m pip install --require-hashes -r requirements.txt   # Windows
.venv/bin/python -m pip install --require-hashes -r requirements.txt       # macOS / Linux
cp .env.example .env
```

Open `.env` and fill in your own values; [configuration](docs/configuration.md) explains each
setting. `.env` is git-ignored and must never be committed.

The command that starts the server will be added here once the server is built. When it runs,
the UI is at <http://127.0.0.1:8000>. In this build ATLAS listens only on your own machine,
because the demo identity is a role switcher rather than a real sign-in. How an organisation
would run it on its own network is described in [deployment](docs/deployment.md).

To run the tests: install `requirements-dev.txt` the same way, then run `python -m pytest`.

## About this project

ATLAS was built as a team capstone project by Swetha, Bhuneshwari and Anagha.

## Licence

Not yet chosen.
