# Architecture

ATLAS watches an engineering organisation's delivery systems and answers two questions:

- **At release close:** what technical debt and issues will the next release inherit, and who
  should take each one: a developer, a coding agent, or a human decision?
- **Mid-release:** why did this failure happen, what does it block, and what should happen next?

It never decides scope, staffing or dates, never writes outside a sandbox branch, and never
states anything without a citation.

## Components

```
trigger (release close, failure, nightly, question; automatic or manual)
  -> admission (one transaction: event key, snapshot, run id, dispatch intent)
  -> input guardrail (flag instruction-like text; redact before any model call)
  -> router (plain code)
  -> Scout (investigator agent)  <->  read-only tools over the run's snapshot
  -> forecast and workforce services (calculations, not agents)
  -> Sentinel (verifier agent; sees only the claims and the records they cite)
  -> verified artefact (anything bound to an unsupported claim is removed)
  -> projection per reader  ->  reports, API, live events, notifications
  -> (delegated tasks only) Approval A -> sandboxed coding agent -> Approval B
     -> draft pull request on the sandbox repository -> CI -> human adjudication
```

### Evidence

Every source record is stored with its provenance (observed, synthetic or derived), the time the
content became true, the time through which it was confirmed (used for freshness), the access
scopes of its source and the records it was derived from. One writer per source. An older
revision never overwrites a newer one. Two sources that disagree become an "insufficient
evidence" item; no agent may resolve it by choosing a side.

Runs are bound to an immutable **snapshot**. Every read, search and graph traversal can reach only
that snapshot. Live fetches go to a separate live store, which is frozen into new snapshots on
request. Evidence that arrives after a pull request is published (CI results, behavioural checks,
human adjudication) lives in a separate validation store keyed by the published action, and
security scans for a task live in their own partition, so neither can leak into an investigation.

### Agents

ATLAS has two AI agents, named Scout and Sentinel. They are software agents, not people.

- **Scout** is the investigator. It reads the evidence, chooses what to read next from what it
  has just read, finds the likely cause of a failure, and builds the cited findings behind the
  next-release debt forecast.
- **Sentinel** is the verifier. It independently checks every claim, in findings, forecast items
  and allocation rationale, against the records it cites before anything is published. It never
  sees Scout's reasoning, so it cannot simply agree with it.

Everything else (routing, ranking the forecast, allocation, the security gate) is ordinary
code.

### Safety controls

- Retrieved text is data, never instructions; tool allow-lists are enforced outside the model.
- One outbound boundary redacts secrets, host names and personal data before any model call and
  counts every outbound attempt; in Offline mode every attempt is refused.
- One projection function is the only way content leaves the backend; it removes every field
  the reader is not entitled to see, and anything with unknown scopes.
- Code changes need two approvals with expiry, bound to the exact diff, the verification
  generation, the security decision and the destination; both are re-checked before every write.
- Containment switches stop model calls, preparation or publication when a safety rule is
  broken, and resume only with recorded, incident-specific prerequisites.

### Data labels

Every response states whether its data is `REAL`, `SYNTHETIC` (team-built and marked), `STAGED`
(the code path exists but the real-world piece is outside this build) or `UNEVALUATED` (a quality
bar that this build cannot measure). Nothing is shown as more real than it is.

## Interfaces

- HTTP API: see `schema/api.md`.
- Data shapes: JSON schemas in `schema/`.
- Live events: `schema/events.json`.
- Notification catalog: `schema/notification_events.json`.
- Configuration: `docs/configuration.md`.
