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

## Parts

A run is one piece of ATLAS work, such as one investigation. Each run passes through these
parts in order:

```
trigger: end of release, failure, nightly sweep or question; automatic or manual
  -> admission: records the event, the snapshot, the run id and the plan to start it, in one step
  -> input check: marks text that looks like an instruction; removes secrets before any AI call
  -> router: plain code that picks the kind of run
  -> Scout (investigator agent)  <->  read-only tools over the run's snapshot
  -> forecast and staffing services: plain calculations, not agents
  -> Sentinel (verifier agent): sees only the claims and the records they cite
  -> verified result: everything that depends on a rejected claim is removed
  -> per-reader filter -> reports, web interface, live updates, notifications
  -> delegated tasks only: Approval A -> coding agent in a container -> Approval B
     -> draft pull request on the sandbox repository -> automated tests -> a person's review
```

### Evidence

ATLAS stores each record from a source with these details:

- Where the record came from: observed, synthetic (made up by the team) or derived (built from
  other records).
- The time the content became true.
- The time up to which ATLAS confirmed it. ATLAS uses this time to decide whether the record is
  still fresh.
- Who may read it. ATLAS copies the source's access rules.
- The records it was built from, if any.

Each source has one writer. An older version of a record never replaces a newer one. If two
sources disagree, ATLAS records "insufficient evidence". No agent may settle the disagreement
by picking a side.

Each run reads one snapshot. A snapshot is a frozen copy of the data that never changes. Every
read, search and graph lookup in a run can reach only that snapshot. Live data goes into a
separate live store. ATLAS turns the live store into a new snapshot when asked.

Some evidence arrives after ATLAS publishes a pull request. Examples are test results, the
behaviour checks and a person's review. This evidence goes into a separate validation store,
keyed by the published task. Security scans for a task go into their own separate area. Neither
can reach an investigation.

### Agents

An agent is a program that uses an AI model to choose its next step. ATLAS has two agents,
named Scout and Sentinel. They are software agents, not people.

- **Scout** is the investigator. It reads the evidence and chooses what to read next. It finds
  the likely cause of a failure. It writes the findings behind the next-release debt forecast,
  and cites a record for each one.
- **Sentinel** is the verifier. It checks every claim against the records that the claim cites.
  It checks findings, forecast items and the reasons for each staffing suggestion. It does this
  before anything is published. Sentinel never sees Scout's reasoning, so it cannot simply agree
  with Scout.

Everything else is plain code. This includes routing, ranking the forecast, splitting the work
and the security check.

### Safety controls

- **Retrieved text is data.** ATLAS never follows instructions that it finds in logs, tickets
  or commit messages. Code outside the AI model controls which tools an agent may use.
- **One exit for outgoing data.** All data leaving ATLAS for an AI model passes one point. That
  point removes secrets, server names and personal data. It counts every attempt to send data
  out. In Offline mode, it refuses every attempt.
- **One filter for readers.** All content leaving the server passes one filter. The filter
  removes every field that the reader may not see. It also removes anything whose access rules
  are unknown.
- **Two approvals for code changes.** Each approval has an expiry time. Each approval is tied to
  the exact change, the version of the verified result, the security decision and the target
  repository. ATLAS checks both approvals again before every write.
- **Containment switches.** A safety violation trips a switch. The switch stops AI calls,
  change preparation or publishing. Work resumes only after the conditions recorded for that
  incident are met.

### Data labels

Every response says where its data came from:

| Label | Meaning |
|---|---|
| `REAL` | Real public data and real AI model calls. |
| `SYNTHETIC` | Data that the team made up. ATLAS marks it. |
| `STAGED` | The code exists, but the real-world part is not in this build. |
| `UNEVALUATED` | A quality target that this build cannot measure. |

ATLAS never shows data as more real than it is.

## Interfaces

- API routes (the web addresses the user interface calls): `schema/api.md`.
- Data formats: the JSON schemas in `schema/`.
- Live update events: `schema/events.json`.
- Notification list: `schema/notification_events.json`.
- Settings: `docs/configuration.md`.
