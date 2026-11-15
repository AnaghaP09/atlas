# ATLAS guide

This guide explains ATLAS in plain words. Read this page first. Then open the page for your role.

## What ATLAS is

ATLAS helps a software team plan its next release. It reads the project's issues, code history,
test results and security scans. It then does two jobs:

- **Release close.** At the end of a release, ATLAS lists the technical debt and open issues
  that the next release is likely to inherit. Technical debt is work that a team put off and
  must do later. ATLAS also suggests who should take each item.
- **Investigation.** When a test or build fails during a release, ATLAS looks for the likely
  cause and shows the evidence.

ATLAS suggests. People decide. ATLAS never sets scope, staffing or dates.

## The two agents

An agent is a program that uses an AI model to choose its next step. ATLAS has two. They are
software, not people.

| Agent | Job |
|---|---|
| **Scout** | Reads the evidence and finds the likely cause of a failure. Writes the findings behind the forecast. Cites a record for every finding. |
| **Sentinel** | Checks every claim against the records it cites, before anything is published. Never sees Scout's reasoning. |

If Sentinel rejects a claim, ATLAS removes the claim and everything that depends on it.

## How a release close works

1. A run starts. It starts on its own when a new release tag appears, or a person starts it on
   the [Run now](run-now.md) page.
2. ATLAS uses a frozen copy of the project data. The screen shows it as **Data as of** and a
   date. See [Data as of](data-as-of.md).
3. Scout reads that copy and writes its findings.
4. Plain code ranks the items and suggests who should take each one.
5. Sentinel checks every claim.
6. ATLAS publishes the reports. Each role sees its own report on the [Reports](reports.md) page.
7. People make the decisions and record them on the [Record a decision](record-a-decision.md) page.

## How an investigation works

1. A failure appears, or a person asks ATLAS to look at one.
2. Scout reads the logs, the recent changes and the related issues.
3. Scout names the likely cause, or says that the evidence is not enough.
4. Sentinel checks every claim.
5. ATLAS publishes the investigation report and suggests an action.
6. If the action is a small code fix, an engineering manager can hand it to the coding agent.
   This needs two approvals. See [Approvals](approvals.md).

## Pages for each role

| Role | Page |
|---|---|
| Product manager | [Product manager](product-manager.md) |
| Engineering manager | [Engineering manager](engineering-manager.md) |
| Solution architect | [Solution architect](solution-architect.md) |
| Security engineer | [Security engineer](security-engineer.md) |
| On-call engineer | [On-call engineer](on-call-engineer.md) |
| Audit | [Audit](audit.md) |

## Pages for each screen

- [Home](home.md)
- [Live dashboard](live-dashboard.md)
- [Run now](run-now.md)
- [Automatic runs](automatic-runs.md)
- [Runs](runs.md)
- [Reports](reports.md)
- [Needs a person](needs-a-person.md)
- [Approvals](approvals.md)
- [Tasks](tasks.md)
- [Notifications](notifications.md)
- [Kill switches](kill-switches.md)
- [Scorecard](scorecard.md)
- [Knowledge graph](knowledge-graph.md)
- [Record a decision](record-a-decision.md)
- [Quality checks](quality-checks.md)
- [Guided demo](guided-demo.md)
- [Sign-in and projects](sign-in-and-projects.md)
- [Add project](add-project.md)

## More help

- [System and mode](system-and-mode.md): health, Connected and Offline, and spend.
- [Access requests](access-requests.md): how to ask to read a locked item.
- [Data as of](data-as-of.md): why ATLAS works from a frozen copy of the data.
- [Glossary](glossary.md): the meaning of each term.
- [Install ATLAS on your own machine](install.md).

## What is not in this build

- A real company sign-in. This build has demo users only.
- An AI model that runs inside your own network. Offline mode blocks all outgoing connections,
  but AI steps then stop.
- Delivery to a real chat workspace. ATLAS can send to a chat address if you set one.
- Use by several computers. ATLAS runs on one machine and accepts connections only from it.
