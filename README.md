# ATLAS

ATLAS helps software teams plan their next release. Teams that ship on a fixed release schedule
can use it.

At the end of each release, ATLAS looks at the open problems. It lists the technical debt and
issues that the next release is likely to inherit. Technical debt is work that a team put off
and must do later. For each item, ATLAS suggests who should take it: a developer, a coding
agent, or a person who must make a decision. A coding agent is a program that writes code
changes.

During a release, ATLAS also looks into failures. It finds the likely cause and shows the
evidence for it.

ATLAS makes suggestions. People make the decisions. ATLAS never sets scope, staffing or dates.
It never changes code outside a separate sandbox branch. It never states a fact without a
citation. A citation is a link to the record that supports the fact.

## Who uses it

| Role | What ATLAS gives them | What they decide |
|---|---|---|
| Product manager | A forecast of the debt and risks in the next release | Which debt goes into the release, how much time to spend on debt, what to cut, and the release date |
| Engineering manager | A plan that splits the work between developers and coding agents, and reports on failures | What to hand to a coding agent, what to give to a developer, and where more people are needed |
| Solution architect | A report on the parts of the system with the most debt | Which debt to accept, with a written reason, and which debt to fix |

Security engineers, on-call engineers and component owners also get reports and notifications.

## How it works

1. **A run starts.** A run is one piece of ATLAS work, such as one investigation. A run starts
   on its own when a new release tag appears, when a new failure appears, or each night. A
   person can also start a run from the user interface. Both kinds of run follow the same rules,
   limits and audit record.
2. **ATLAS collects evidence.** Connectors read the issue tracker, the Git history, the test
   system, a security scanner and a map of the system's parts. A connector is a small program
   that reads one source. In the demo, cluster events, error rates and staffing data are made-up
   data. Each run reads one frozen copy of the data, called a snapshot. You can repeat a run and
   get the same inputs.
3. **Two AI agents do the reasoning.** An agent is a program that uses an AI model to decide
   its next step. Scout and Sentinel are software agents, not people.
   - **Scout** is the investigator. It reads the evidence and chooses what to read next. It
     finds the likely cause of a failure. It writes the findings behind the debt forecast, and
     cites a record for each one.
   - **Sentinel** is the verifier. It checks each claim against the records that the claim
     cites. It does this before anything is published. Sentinel never sees Scout's reasoning,
     so it cannot simply agree with Scout.

   If Sentinel rejects a claim, ATLAS removes that claim and everything that depends on it.
4. **Ordinary code does the calculations.** Ranking the forecast, splitting the work and the
   security check are plain code, not agents. They use only the findings that Sentinel accepted.
5. **People approve every code change.** An engineering manager must approve a task twice
   before a coding agent's change goes anywhere. The first approval lets the agent prepare the
   change. The second approval covers the exact change. The coding agent works inside a
   container with no network access and no secrets. A container is an isolated space on the
   computer. ATLAS then opens a draft pull request on a separate repository,
   [atlas-sandbox-target](https://github.com/AnaghaP09/atlas-sandbox-target). A pull request
   is a proposed change that a person reviews. ATLAS never merges a change.
6. **ATLAS tells people what happened.** Every error, approval request and safety event creates
   a notification. ATLAS sends each notification to the roles that need it. Notifications appear
   in the user interface. They can also go to a chat channel.

More detail: [architecture](docs/architecture.md), [data sources](docs/data-sources.md),
[configuration](docs/configuration.md), [deployment](docs/deployment.md),
[security](SECURITY.md).

## Status

ATLAS is still being built. The shared data formats, interfaces and settings are done. The
connectors, agents, code-change steps, the API and the user interface are in progress. The API
is the set of web addresses that the user interface calls. This
page will say when each part works. Until then, read everything here as the design.

The demo labels each result with the source of its data:

| Label | Meaning |
|---|---|
| `REAL` | Real public data and real AI model calls. |
| `SYNTHETIC` | Data that the team made up. ATLAS marks it and never counts it as a real result. |
| `STAGED` | The code exists, but the real-world part is not in this build. One example is a self-hosted AI model for offline use. |
| `UNEVALUATED` | A quality target that this build cannot measure. |

ATLAS works with any software project. The demo and the tests use public data from
[KubeVirt](https://github.com/kubevirt/kubevirt), an open-source project.

KubeVirt does not record which issues blocked each release. So ATLAS measures its forecast
accuracy against a substitute definition of a blocking issue. The
[data sources](docs/data-sources.md) page gives the definition. Every accuracy result says that
it uses this substitute.

Two additions are in progress:

- **A second public project.** ATLAS is being extended to
  [Argo CD](https://github.com/argoproj/argo-cd). This shows that ATLAS is not tied to one
  project. Argo CD appears only in the live views. The quality tests still use KubeVirt.
- **A dashboard that refreshes itself.** The dashboard checks GitHub for changes at a fixed
  interval. The screen shows the interval. The data is as recent as the last check, so it is
  close to real time but not instant.

## Quick start

You need Python 3.14, Node.js 24 and Docker. Docker runs the coding agent's container.

```
git clone https://github.com/AnaghaP09/atlas.git
cd atlas
python -m venv .venv
.venv/Scripts/python -m pip install --require-hashes -r requirements.txt   # Windows
.venv/bin/python -m pip install --require-hashes -r requirements.txt       # macOS / Linux
cp .env.example .env
```

Open `.env` and enter your own values. The [configuration](docs/configuration.md) page explains
each setting. Git ignores `.env`. Never commit it.

The command that starts the server does not exist yet. This page will show it when the server
is built. The user interface will then be at <http://127.0.0.1:8000>. In this build, ATLAS
accepts connections only from your own computer. The reason is that the demo has no real
sign-in. It uses a role switcher instead. The [deployment](docs/deployment.md) page explains how
an organisation would run ATLAS on its own network.

To run the tests, install `requirements-dev.txt` in the same way. Then run `python -m pytest`.

## About this project

ATLAS was built as a capstone project.

## Licence

MIT (see LICENSE).
