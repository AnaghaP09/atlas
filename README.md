# ATLAS

**Documentation:** <https://anaghap09.github.io/atlas/> (the user guide as a website: install, each role, reports and approvals)

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

## Key highlights

**What ATLAS does today**

- **Forecasts the debt the next release inherits.** At the end of a release, ATLAS lists the technical debt and
  issues likely to carry over, per component, each with a citation to the record behind it.
- **Proposes who should take each item.** It splits the work between developers, coding agents and a person who must
  decide first, with a short reason for each item.
- **Investigates failures.** Scout, the investigating agent, reads the evidence, finds the likely cause and cites a
  record for every claim.
- **Checks every claim before anyone sees it.** Sentinel, the verifying agent, checks each claim against the records it
  cites, without seeing Scout's reasoning. ATLAS removes any claim Sentinel rejects.
- **Keeps people in charge of code changes.** A coding agent works only after an engineering manager approves, inside
  a container with no network access and no secrets. ATLAS never merges a change.
- **Shows the evidence behind every number.** Each run reads one frozen copy of the data, shown as "Data as of" a date,
  so a run can be repeated with the same inputs.
- **Stays safe to run.** A security gate checks dependencies, kill switches stop any part of ATLAS at once, and
  Offline mode makes no outside connections.
- **Gives each role its own view.** The product manager, engineering manager and solution architect each see the
  reports that answer their questions; a knowledge graph shows how the system's parts connect.
- **Shows what will stop the release.** Ship readiness lists the blockers now, the services they affect, and whether
  the team can still deliver.
- **Turns approved fixes into draft pull requests.** After the engineering manager approves, ATLAS opens a draft pull
  request on a sandbox repository and reports the test results back. A person decides whether to merge.
- **Connects the product manager to the team.** The product manager sends a feature request to the engineering manager
  or a technical dependency to the solution architect, or hands over an investigation, and follows it to the result.
- **Adds any public project from a link,** with no custom integration.

**Coming in the next version**

- A stricter two-step approval that a company can switch on, where a person approves the exact change.
- Each role's own view of the forecast: release risk, who takes what, and the parts of the system that keep decaying.
- Forecast across releases: earlier forecasts compared with what really happened.
- A detailed view of how Scout investigates and how Sentinel checks each claim.
- The full path from a request to the finished change, shown on every page.

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
   data. Each run reads one frozen copy of the data. The interface shows it as "Data as of" a date. You can repeat a run and
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
   computer. ATLAS is then meant to open a draft pull request on a separate repository,
   [atlas-sandbox-target](https://github.com/AnaghaP09/atlas-sandbox-target). A pull request
   is a proposed change that a person reviews. ATLAS never merges a change.
6. **ATLAS tells people what happened.** Every error, approval request and safety event creates
   a notification. ATLAS sends each notification to the roles that need it. Notifications appear
   in the user interface. They can also go to a chat channel.

More detail: [architecture](docs/architecture.md), [data sources](docs/data-sources.md),
[configuration](docs/configuration.md), [deployment](docs/deployment.md),
[security](SECURITY.md).

## Status

ATLAS runs on one computer. You can start it, open the user interface and try it with real
public data. It is not finished. The table shows what works today.

| Part | State |
|---|---|
| Scout and Sentinel, the forecast and the three planning reports | Works with real data and real AI model calls. |
| Investigation of a reported failure | Works. Results still vary between runs. |
| User interface with a menu for each role | Works. |
| Live dashboard for two public projects | Works. It checks GitHub at a fixed interval and shows the interval. |
| Access requests and kill-switch drills | Work. |
| Sign-in over HTTPS on your own computer | Works after a one-time setup. See the [guide](docs/guide/install.md). |
| Coding agent in a container, with two approvals | Works up to the second approval. |
| Draft pull request in a test repository | Not connected yet. |
| Quality tests against the targets | Not run yet. No result is claimed. |
| Self-hosted AI model for offline use | Not in this build. Offline mode blocks all outside connections. |

The user interface labels each result with the kind of data behind it:

| Label | Meaning |
|---|---|
| Real data | Real public data and real AI model calls. |
| Sample data | Data made up for the demo. ATLAS marks it and never counts it as a real result. |
| Not in this demo | The code exists, but the real-world part is not in this build. |
| Not measured | A quality target that this build cannot measure, or has not measured yet. |

ATLAS works with any software project. The demo and the tests use public data from
[KubeVirt](https://github.com/kubevirt/kubevirt), an open-source project. A second public
project, [Argo CD](https://github.com/argoproj/argo-cd), appears in the live views to show that
ATLAS is not tied to one project.

KubeVirt does not record which issues blocked each release. So ATLAS measures its forecast
accuracy against a substitute definition of a blocking issue. The
[data sources](docs/data-sources.md) page gives the definition. Every accuracy result says that
it uses this substitute.

## Quick start

You need Python 3.14, Node.js 24 and Docker. Docker runs the coding agent's container.

```
git clone https://github.com/AnaghaP09/atlas.git
cd atlas
python -m venv .venv
.venv/Scripts/python -m pip install --require-hashes -r requirements.txt   # Windows
.venv/bin/python -m pip install --require-hashes -r requirements.txt       # macOS / Linux
python -m atlas.setup      # writes .env and asks for your values
cd ui && npm ci --ignore-scripts && npm run build && cd ..
python -m atlas.doctor     # checks your setup and explains any problem
python -m atlas.serve      # starts ATLAS
```

Then open <http://127.0.0.1:8000>. The [user guide](https://anaghap09.github.io/atlas/) explains each screen and
each role. The [configuration](docs/configuration.md) page explains each setting. Git ignores
`.env`. Never commit it.

In this mode, ATLAS accepts connections only from your own computer and uses a role switcher in
place of sign-in. The [install guide](docs/guide/install.md) shows how to turn on sign-in over
HTTPS. The [deployment](docs/deployment.md) page explains how an organisation would run ATLAS on
its own network.

To run the tests, install `requirements-dev.txt` in the same way. Then run `python -m pytest`.

## About this project

ATLAS was built as a capstone project.

## Licence

MIT (see LICENSE).
