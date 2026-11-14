# ATLAS web interface

The web interface for ATLAS. It is a React and TypeScript app. It talks to the ATLAS backend only
through the backend's HTTP API under `/api`. The backend serves the built app from `ui/dist/` at
`/`, on the same address, so no second port and no cross-site requests are needed.

## What it shows

- Home: mode, components, recent runs and unread notifications.
- Run now: start a release close, an investigation, the nightly sweep, a question, or a fresh
  check of a stored run. The server decides what is available and says why when it is not.
- Automatic runs: the watchers and schedules, with enable, pause and run once.
- Runs and reports: live progress, reports with their claims, and the evidence behind each claim.
- Needs a person, Approvals, Tasks, Notifications, Kill switches and Scorecard.
- Knowledge graph, Record a decision, Quality checks and the Guided demo.

Every view shows whether its data is real, synthetic (made up for the demo), not available in this
demo, or not evaluated. The two AI agents are named in the interface: Scout investigates and
Sentinel checks every claim.

## Requirements

Node.js 24 and npm 11.

## Install

```sh
cd ui
npm ci --ignore-scripts
```

Install scripts are turned off for every package.

## Commands

| Command | What it does |
|---|---|
| `npm run dev` | Development server on `127.0.0.1:5173`. It passes `/api` to the backend at `ATLAS_API_URL` (default `http://127.0.0.1:8000`). |
| `npm run build` | Type check and build into `dist/`. |
| `npm test` | Unit and component tests. |
| `npm run lint` | Lint. |
| `npm run api:types` | Regenerate `src/api/schema.ts` from `openapi.json`. |
| `npm run api:check` | Fail if `openapi.json` or `src/api/schema.ts` no longer match the backend. Set `ATLAS_PYTHON` to the Python that runs the backend. |
| `npm run e2e` | Browser tests. Set `ATLAS_E2E_URL` and run `npx playwright install chromium` once. |

To refresh the API description after a backend change:

```sh
python ui/scripts/export_openapi.py > ui/openapi.json
cd ui && npm run api:types
```

## Development server settings

| Setting | Default |
|---|---|
| `ATLAS_UI_DEV_HOST` | `127.0.0.1` |
| `ATLAS_UI_DEV_PORT` | `5173` |
| `ATLAS_UI_DEV_ALLOWED_HOSTS` | `127.0.0.1:5173,localhost:5173,[::1]:5173` |
| `ATLAS_API_URL` | `http://127.0.0.1:8000` |

The development server refuses any other Host value and any other browser Origin.

## Identity in this build

This build uses a demo identity: you choose a role from the list at the top right. It is not
sign-in. Changing the role, or the project, starts fresh: requests in flight are cancelled, live
updates are closed, and nothing shown for the previous role stays on screen.
