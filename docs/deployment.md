# Deploying ATLAS for an organisation

This page describes how an organisation would run ATLAS in a pilot. **This is the design. This
build does not do it yet.**

This build accepts connections only from the computer it runs on, at
`http://127.0.0.1:8000`. It refuses every other network address. The reason is that the demo
has no real sign-in. It uses a role switcher instead. ATLAS can open to a network only after it
has the single sign-on described below.

## Where it runs

The organisation installs ATLAS inside its own network, next to the systems that ATLAS reads.
There are two ways to install it:

- **On one server.** Docker Compose runs the ATLAS server, its stores and the coding agent's
  container on one machine. Docker Compose is a tool that starts several containers together.
- **On Kubernetes.** Helm charts install the same parts on the organisation's cluster. A Helm
  chart is a package that describes how to install software on Kubernetes. The organisation
  keeps internal copies of the container images, software packages and security-flaw lists.

Users open ATLAS at an internal address that the organisation chooses, such as
`https://atlas.company.internal`. Users do not install anything on their own computers.

## Who sees what

Users sign in with the organisation's single sign-on. Single sign-on lets people use one company
login for many tools. Each user has one role: product manager, engineering manager, solution
architect, security engineer, on-call engineer or audit. Each role sees only what it is allowed
to see.

Each item that ATLAS builds keeps the access rules of every source behind it. If a user lacks
access to any of those sources, the user does not see the item. Each report says how many items
it held back.

## Connected and Offline modes

| Mode | AI model | Network |
|---|---|---|
| Connected | An approved AI model service, reached at an allowed address. ATLAS removes secrets and personal data from every message first. | Only allowed addresses. |
| Offline | An AI model that runs inside the organisation's network. | No outgoing connections. ATLAS refuses and counts every attempt. |

This build shows the Offline rule: zero outgoing attempts, and AI steps stop safely. It does not
include a self-hosted AI model. That part has the label `STAGED`.

## How people get results

- **In the ATLAS user interface:** reports, approvals, notifications and the scorecard.
- **In team chat:** a link to each report or alert. ATLAS posts it to Slack, or to a chat tool
  that the organisation runs itself, such as Mattermost. Each chat channel states who reads it.
  ATLAS sends only what that group may see.
- **In the organisation's own Git system:** a task given to the coding agent appears as a draft
  pull request on a sandbox branch. This happens only after two approvals from an engineering
  manager. ATLAS never merges anything.
