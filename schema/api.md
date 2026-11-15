# ATLAS HTTP API

All routes are under `/api`. The OpenAPI document is served at `/api/openapi.json`. The web UI is
served at `/` by the same server, and the UI fallback never answers an `/api` path.

## Conventions

- **Identity.** The demo identity is the `X-Atlas-Role` header (`pm`, `em`, `architect`,
  `security`, `oncall`, `audit`). It is a stand-in for single sign-on, so the server only binds to
  a loopback address while it is enabled. No query parameter can widen what a caller sees: every
  response and event is projected for the role in the header.
- **Host and Origin.** Requests must carry a `Host` header from an exact allow-list
  (`127.0.0.1:<port>`, `localhost:<port>`, `[::1]:<port>`). Browser requests carrying `Origin`
  must match it. Anything else is rejected before routing.
- **Idempotency.** Every manual mutation requires an `Idempotency-Key` header (UUID). Repeating a
  request with the same key and payload returns the stored outcome; reusing a key with a
  different payload is rejected.
- **Data labels.** Every response that carries data includes `data_mode` (`REAL`, `SYNTHETIC`,
  `STAGED`, `UNEVALUATED`). Features that are not available in the demo answer
  `status: "not_available_in_demo"` with a reason.
- **Streams.** `GET /api/runs/{id}/stream` and `GET /api/notifications/stream` send server-sent
  events whose names and fields are defined in `events.json`. Clients send the role header, so
  use a fetch-based reader; reconnect with `Last-Event-ID`. When the requested position is older
  than the replay buffer the server sends `stream.reset`, and the client refetches its views.

## Triggers and schedules

| Route | Purpose |
|---|---|
| `GET /api/triggers/catalog` | Manual triggers with parameters, allowed roles and current availability; automatic triggers with state, last and next run |
| `POST /api/triggers/release-close`, `/failure`, `/nightly`, `/question` | Start a run bound to a snapshot; returns `run_id` |
| `POST /api/triggers/manual/investigate` | Investigate a chosen failure or issue on a chosen snapshot |
| `POST /api/runs/{id}/reverify` | Re-run verification; creates a new authoritative generation |
| `GET /api/schedules` | Watchers and schedules with state, last and next run, last event seen |
| `POST /api/schedules/{id}/enable`, `/pause`, `/run-once` | Control a watcher or schedule |

## Data and runs

| Route | Purpose |
|---|---|
| `POST /api/ingest` | Fetch live data into the live store |
| `POST /api/snapshots` | Freeze the live store into an immutable snapshot |
| `GET /api/snapshots?release=`, `GET /api/snapshots/{id}` | Snapshot identities and metadata |
| `GET /api/runs`, `GET /api/runs/{id}` | Run state, status, origin, snapshot, generations, budgets, cost, report links |
| `GET /api/runs/{id}/stream` | Live run events |
| `GET /api/reports?run_id=&type=`, `GET /api/reports/{id}` | Report discovery and the projected report |
| `GET /api/evidence/{record_id}?run_id=&content_version_id=` | Resolve a citation to the exact record version the run saw |
| `GET /api/graph/{snapshot_id}` | Knowledge-graph nodes and edges of one snapshot |
| `GET /api/queue` | Items waiting for a human decision |
| `GET /api/projects/{project_id}/outlook` | Release outlook for the product manager, engineering manager and solution architect: what the next release inherits and who should take it (from the latest verified release close), and what stops shipping now with the delivery status and the rule that set it. `project.outlook_changed` on the project stream says when to refetch. Other roles get 403 |
| `POST /api/feedback` | Record an override or a debt-acceptance rationale |

## Tasks, approvals and publication

| Route | Purpose |
|---|---|
| `POST /api/tasks` | Create a task from a verified delegation; runs the initial security scan |
| `GET /api/tasks`, `GET /api/tasks/{id}` | Lifecycle, approvals, checks, publication and validation results |
| `GET /api/approvals`, `POST /api/approvals/{id}/decision` | Approval A and Approval B |
| `POST /api/tasks/{id}/prepare`, `/publish`, `/adjudicate` | Prepare in the sandbox, publish a draft pull request, record human adjudication |
| `POST /api/tasks/{id}/security-refresh` | Renew the task's security decision |
| `GET /api/scorecard` | Agent reliability scorecard |

## Notifications

| Route | Purpose |
|---|---|
| `GET /api/notifications` | The caller's notifications |
| `GET /api/notifications/stream` | Live notifications |
| `POST /api/notifications/{id}/ack`, `/resolve` | Acknowledge or resolve |
| `GET /api/notifications/dead-letter` | Failed external deliveries |
| `GET /api/destinations`, `POST /api/webhooks/test` | External destinations and their declared audience |

## Mode, containment and corrections

| Route | Purpose |
|---|---|
| `GET /api/mode`, `POST /api/mode` | Connected or Offline |
| `GET /api/containment`, `GET /api/containment/{switch}` | Switches, open incidents, prerequisites, corrections |
| `POST /api/incidents/{incident_id}/prerequisites` | Record a prerequisite for resuming after an incident |
| `POST /api/incidents/{incident_id}/resume` | Resume once every prerequisite is valid |
| `POST /api/corrections/authorisations` | Authorise one correction for one incident |
| `GET /api/corrections?incident_id=` | List corrections, including held ones |
| `POST /api/corrections` | Apply an authorised correction |

## Health, evaluation and demo

| Route | Purpose |
|---|---|
| `GET /api/health` | Liveness, server identity, configuration presence (never values), staged and unevaluated features |
| `GET /api/evaluations`, `/latest`, `/{id}` | Evaluation results for each quality gate |
| `GET /api/demo/manifest` | Ordered demo steps for the guided demo |
| `POST /api/scenario/events`, `/faults`, `/clock`, `/spend` | Scenario server only: inject test events, faults, time and spend for demonstrations |
