# Deploying ATLAS for an organisation

This page describes how ATLAS is designed to run for an organisation in a pilot. **It is a
design, not something this build does.** The current build runs only on the local machine
(`http://127.0.0.1:8000`) and refuses any other network address, because its demo identity is a
role switcher, not a real sign-in. Opening ATLAS to a network requires the single sign-on
described below.

## Where it runs

ATLAS is installed inside the organisation's own network, next to the systems it reads:

- **One server:** the ATLAS server, its stores and the coding-agent sandbox run with Docker
  Compose on a single machine.
- **Kubernetes:** the same components are installed on the organisation's cluster with Helm
  charts, with container images, packages and vulnerability feeds mirrored internally.

People reach the UI at an internal address chosen by the organisation, for example
`https://atlas.company.internal`. Nothing needs to be installed on their machines.

## Who can see what

Users sign in through the organisation's single sign-on. Each person has a role (product
manager, engineering manager, solution architect, security engineer, on-call engineer or audit)
and sees only what that role allows. Every derived item carries the access rights of all the
sources behind it; a reader who lacks any of them does not see the item, and reports state how
many items were withheld.

## Connected and Offline modes

| Mode | Model calls | Network |
|---|---|---|
| Connected | An approved model provider over an allow-listed endpoint; every outbound payload is redacted first | Only allow-listed endpoints |
| Offline | A self-hosted model inside the network | No outbound connections at all; every attempt is refused and counted |

In this build, Offline mode demonstrates the guarantee (zero outbound attempts, model steps fail
closed) but does not include a self-hosted model; that part is labelled `STAGED`.

## How people receive results

- **The ATLAS UI:** reports, approvals, notifications and the scorecard.
- **Team chat:** a link to each report or alert, posted to Slack or a self-hosted chat such as
  Mattermost. Each chat destination declares who reads it, and ATLAS sends only what that
  audience may see.
- **The organisation's own Git system:** delegated tasks appear as draft pull requests on a
  sandbox branch, after two engineering-manager approvals. ATLAS never merges anything.
