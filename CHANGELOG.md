# Changelog

This page lists what ATLAS can do, newest first. Each entry describes something you can see or
use. The [README](README.md) has the current status of each part.

## Not yet working

- **Draft pull request in the test repository.** A coding task reaches the second approval.
  Opening the pull request is not connected.
- **Quality tests.** The tests that score ATLAS against its targets have not been run. No result
  is claimed.
- **Self-hosted AI model for offline use.** Offline mode blocks every outside connection. This
  build has no model that runs inside your network.
- **Steady results.** An investigation of the same failure can give a different answer on a
  second run.

## 2026-11-15

### Setup

- The README status table and quick start now match what is built.

## 2026-11-14

### Agents

- **Scout and Sentinel.** Scout investigates a failure and writes findings with citations.
  Sentinel checks each claim against the records it cites. Anything that depends on a rejected
  claim is removed before a report is shown.
- **Release close.** ATLAS forecasts which open issues the next release will inherit. It writes
  three reports: for the product manager, the engineering manager and the solution architect.
- **Proposed owner.** Each forecast item gets a proposed owner, with a reason: a coding agent, a
  developer, or a person who must decide.
- **Investigations.** An investigation ends with one of six causes and a next action. Scout can
  read a long record in parts and can filter a test log.
- **Limits.** A run stops at its step, tool-call and time limits. Tool results are cut to a
  fixed size so a run stays within its cost.
- **Automatic and manual runs.** A scheduler starts runs for a new release, new failures and the
  nightly sweep. A person can start the same runs from the interface.

### Data

- **Frozen data copies.** The repository includes 24 copies of KubeVirt data: five releases,
  seven example cases, and a test copy of each with names disguised.
- **Only what was known then.** Each example case is rebuilt as it stood when the failure was
  reported. Later edits, fixes and closing comments are hidden.
- **System map.** ATLAS builds a map of components and their links from Go import statements.
- **Two projects.** KubeVirt and Argo CD each have their own data, runs and notifications. You
  define projects in a file.
- **Live dashboard data.** ATLAS checks GitHub at a fixed interval for open issues, failing test
  runs and the latest release. It works without a GitHub token at a slower rate.
- **Security scans.** ATLAS asks OSV.dev for known flaws in a project's Go modules. Each scan is
  recorded, including a clean one.
- **No personal data.** Names and user handles in saved data are replaced with stand-ins. Email
  addresses, images and access tokens are removed.

### Interface

- **Web interface.** A redesigned interface with light and dark themes.
- **A menu for each role.** The server decides which pages each of the six roles sees. Each role
  has its own home page.
- **Live numbers.** The home page tiles, the spend meter and the notification count change
  without a reload.
- **Readable names.** Data copies, runs and reports are shown by name and date, not by id.
- **System map view.** The map has search, zoom, team colours and a list view.
- **Links between pages.** A run links to its reports and tasks. A report item links to the
  record it cites.
- **User guide.** Each page has a help link that opens the guide inside the interface.
- **Guided demo.** A screen lists the demo steps in order and says what each one shows.

### Safety

- **Access requests.** A locked item can be requested. Ordinary items unlock at once as
  read-only for 24 hours. Security findings and audit pages wait for a decision.
- **Kill switches and drills.** Eleven switches stop AI calls, change preparation or publishing
  after a safety violation. The audit role can run a drill on any switch.
- **Instructions hidden in data.** A small AI model judges text that looks like an instruction
  to an AI. A flagged record is set aside and no agent reads it.
- **Two approvals.** A coding task needs two approvals from the engineering manager. Each one is
  tied to the exact change and expires.
- **Sandbox.** The coding agent's change is built and tested in a container with no network and
  no secrets.
- **Removal of sensitive data.** Secrets, server names and personal data are removed before
  anything is sent to an AI model.
- **Spend ceiling.** Runs stop when model spending reaches the limit you set. Spending is saved
  across restarts, and each run shows its own cost.
- **Notifications.** Every error creates a notification for the roles that need it.

### Setup

- **Settings file.** All settings live in `.env`. Nothing in the code is tied to one computer.
- **Setup and doctor.** `python -m atlas.setup` writes the settings file.
  `python -m atlas.doctor` checks your setup and explains any problem.
- **One command to start.** `python -m atlas.serve` starts the server and the interface.
- **Sign-in over HTTPS.** A one-time setup creates local certificates and six demo users, one
  for each role.
- **Swap points for a pilot.** The AI model provider, the sign-in provider and the sandbox
  runner each sit behind an interface.
- **Locked dependencies.** Python and JavaScript packages are pinned to exact versions and
  audited for known flaws.
- **Checks on every change.** GitHub runs the tests, a check for secrets and personal data, and
  the dependency audit on Windows and Linux.
