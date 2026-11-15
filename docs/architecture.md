# Architecture

ATLAS reads the systems that a software team uses to deliver releases. It answers two questions:

- **At the end of a release:** Which technical debt and issues will the next release inherit?
  Who should take each one: a developer, a coding agent, or a person who must decide?
- **During a release:** Why did this failure happen? What does it block? What should happen
  next?

Technical debt is work that a team put off and must do later. A coding agent is a program that
writes code changes.

ATLAS never decides scope, staffing or dates. It never writes code outside a sandbox branch. It
never states a fact without a citation. A citation is a link to the record that supports a fact.

## How a run works

A run is one piece of ATLAS work, such as one investigation. Each run passes through these
parts in order:

```
trigger: end of release, failure, nightly sweep or question; automatic or manual
  -> admission: records the event, the data copy, the run id and the plan to start it, in one step
  -> input check: sets aside text that tries to instruct the AI; removes secrets before any AI call
  -> router: plain code that picks the kind of run
  -> Scout (investigator agent)  <->  read-only tools over the run's data copy
  -> forecast and staffing services: plain calculations, not agents
  -> Sentinel (verifier agent): sees only the claims and the records they cite
  -> verified result: everything that depends on a rejected claim is removed
  -> per-reader filter -> reports, web interface, live updates, notifications
  -> delegated tasks only: Approval A -> coding agent in a container -> Approval B
     -> draft pull request on the test repository -> automated tests -> a person's review
```

The last line is partly built. A task reaches Approval B today. Opening the draft pull request
is not connected yet.

## Triggers

A run starts in one of two ways. Both ways enter through the same admission step, with the same
limits and the same audit record.

| Way | How it starts |
|---|---|
| Automatic | A scheduler inside the server watches for a new release tag, new failures and the nightly sweep time. Each watcher can be switched on, paused or run once. |
| Manual | A person starts a run from the **Run now** screen. The server lists which runs each role may start. |

Each run records who or what started it: a schedule, a watcher or a person.

## Agents

An agent is a program that uses an AI model to choose its next step. ATLAS has two agents,
named Scout and Sentinel. They are software agents, not people.

- **Scout** is the investigator. It reads the evidence and chooses what to read next. It finds
  the likely cause of a failure. It writes the findings behind the next-release forecast, and
  cites a record for each one.
- **Sentinel** is the verifier. It checks every claim against the records that the claim cites.
  It does this before anything is published. Sentinel never sees Scout's reasoning, so it cannot
  simply agree with Scout.

Everything else is plain code. This includes routing, ranking the forecast, proposing an owner
for each item and the security check.

## Evidence and "Data as of"

ATLAS stores each record from a source with these details:

- Where the record came from: observed, sample (made up for the demo) or derived (built from
  other records).
- The time the content became true.
- The time up to which ATLAS confirmed it. ATLAS uses this time to decide whether the record is
  still fresh.
- Who may read it. ATLAS copies the source's access rules.
- The records it was built from, if any.

Each source has one writer. An older version of a record never replaces a newer one. If two
sources disagree, ATLAS records "insufficient evidence". No agent may settle the disagreement
by picking a side.

Each run reads one frozen copy of a project's data. The interface shows it as **Data as of** a
date, for example "KubeVirt at release v1.9.0". A frozen copy never changes. Every read, search
and graph lookup in a run can reach only that copy. This keeps forecasts fair, because a run
cannot see what happened later. It also keeps reports repeatable.

Live data goes into a separate live store for each project. The live dashboard reads it.

Evidence that belongs to one coding task is kept apart from investigations. This covers the
task's security scan, its test results and a person's review.

## Projects

A project is a set of read-only sources: a repository, an issue query, a test system, a list of
software modules and a release tag pattern. You define projects in a file. ATLAS keeps each
project's data, runs, tasks and notifications separate. Before any action, the server checks
the caller's role in the project that owns the object.

The example file defines two public projects, KubeVirt and Argo CD.

## Roles and access

There are six roles: product manager, engineering manager, solution architect, security
engineer, on-call engineer and audit. The server decides each role's menu and home page.

- **One filter for readers.** All content leaving the server passes one filter. The filter
  removes every field that the reader may not see. It also removes anything whose access rules
  are unknown.
- **Access requests.** An item that a role may not open shows as locked. The person can request
  it. Ordinary items, such as reports, tasks and approvals, unlock at once. Sensitive items wait
  for a decision. These are security findings, the security report, kill-switch incidents and
  audit pages. The security or audit role decides.
- **A grant is narrow.** It is read-only, covers one item and expires, after 24 hours by
  default. It never allows approving, publishing or changing anything. The owning role is told
  and can withdraw it.

## Safety controls

- **Retrieved text is data.** ATLAS never follows instructions that it finds in logs, tickets
  or commit messages. A small AI model judges text that looks like an instruction to an AI. A
  record that it flags is set aside, and no agent reads it. Code outside the AI model controls
  which tools an agent may use.
- **One exit for outgoing data.** All data leaving ATLAS for an AI model passes one point. That
  point removes secrets, server names and personal data. In Offline mode, it refuses every
  outside connection and counts the attempt.
- **Two approvals for code changes.** Each approval expires. Each is tied to the exact change,
  the version of the verified result, the security decision and the target repository. ATLAS
  checks both again before every write.
- **Kill switches.** There are 11. A safety violation trips one. The switch stops AI calls,
  change preparation or publishing. Work resumes only after the steps recorded for that incident
  are done.
- **Drills.** The audit role can trip any switch as a drill. A drill blocks the same actions as
  a real trip and is labelled as a drill. One confirmation resumes it.
- **Spend ceiling.** ATLAS stops runs when model spending reaches the limit you set.

## Notifications

Every error creates a notification. Each one has a severity and goes to the roles that need it.
The interface shows them in an inbox that updates live. Anything that needs a decision also goes
to the **Needs a person** queue. Sending to a chat tool through a webhook is built, but this
build tests it only against a local stand-in.

## Swap points for a pilot

The same core runs in the demo and in an organisation's pilot. Three parts sit behind an
interface, so a pilot can replace them without changing the core.

| Part | In this build | In a pilot |
|---|---|---|
| AI model provider | Claude, over the internet | A model server inside the organisation's network. The code exists. This build tests it only against a local stand-in. |
| Sign-in provider | Built-in sign-in, or a role switcher for development | The organisation's single sign-on. Not built yet. |
| Sandbox runner | A locked-down Docker container on the same computer | A separate runner, such as a Kubernetes job. This build tests it only against a stand-in. |

Data sources are a fourth swap point. A new system, such as a different issue tracker, needs a
new connector and a policy row. The core does not change.

## Data labels

The interface labels every result with the kind of data behind it:

| Label | Meaning |
|---|---|
| Real data | Real public data and real AI model calls. |
| Sample data | Data made up for the demo. ATLAS marks it and never counts it as a real result. |
| Not in this demo | The code exists, but the real-world part is not in this build. |
| Not measured | A quality target that this build cannot measure, or has not measured yet. |

API responses carry the same four labels as `REAL`, `SYNTHETIC`, `STAGED` and `UNEVALUATED`.

## Interfaces

- API routes (the web addresses the user interface calls): `schema/api.md`.
- Data formats: the JSON schemas in `schema/`.
- Live update events: `schema/events.json`.
- Notification list: `schema/notification_events.json`.
- Settings: [configuration](configuration.md).
- Screens and roles: the [user guide](guide/index.md).
