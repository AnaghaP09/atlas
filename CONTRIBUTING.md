# Contributing

This page explains how to set up ATLAS for development, run its checks and prepare a change.

## Set up

You need Python 3.14, Node.js 24 and Docker.

1. Create a Python environment and install the locked packages:

   ```
   python -m venv .venv
   .venv/Scripts/python -m pip install --require-hashes -r requirements.txt       # Windows
   .venv/Scripts/python -m pip install --require-hashes -r requirements-dev.txt
   ```

   On macOS and Linux, use `.venv/bin/python`.

2. Write your settings file and check your setup:

   ```
   python -m atlas.setup
   python -m atlas.doctor
   ```

3. Install and build the interface:

   ```
   cd ui
   npm ci --ignore-scripts
   npm run build
   ```

4. Download the sandbox container images once:

   ```
   python -m prep.images.fetch_images
   ```

   ATLAS never downloads an image while it runs. Tests that need an image are skipped until you
   do this step.

## Run the checks

Run all of these before you commit. GitHub runs the first three on every change.

| Check | Command | Where |
|---|---|---|
| Tests | `python -m pytest` | project folder |
| Repository check | `python scripts/public_check.py --history` | project folder |
| Dependency audit | `python -m pip_audit -r requirements.txt --require-hashes`, and the same for `requirements-dev.txt` | project folder |
| Interface types | `npm run typecheck` | `ui` |
| Interface style | `npm run lint` | `ui` |
| Interface tests | `npm test` | `ui` |
| Interface matches the API | `npm run api:check` | `ui` |
| Interface audit | `npm audit` | `ui` |

Tests never call a real AI model. They use recorded answers.

The repository check scans files and commit messages. It fails on secrets, email addresses,
personal names, paths from one person's computer, and internal planning notes.

## Commit style

- Write one line that says what changed for the product. Example:
  `Add evidence store with revision ordering`.
- Do not mention plans, review rounds, branch names or people.
- Keep each commit to one change.

## Never commit

- `.env`, keys, tokens or passwords.
- Anything under `data/local/`. This holds raw downloads, certificates and user files.
- Personal data. Saved test data must pass the cleaning step that replaces names with stand-ins.
- `ui/dist/` and `node_modules/`.

## Add a dependency

1. Add the package to `requirements.in`, or to `requirements-dev.in` for a test tool.
2. Lock it again with hashes: `pip-compile --generate-hashes`.
3. Run the dependency audit.

For the interface, pin an exact version in `ui/package.json` and commit `ui/package-lock.json`.
Install scripts stay off.

## Writing for the interface and the docs

Keep text in the interface short: labels, numbers and status words. Put explanations in the
[user guide](docs/guide/index.md). Use simple English and define a term the first time it
appears.

## Report a security problem

Do not open a public issue. Follow the [security policy](SECURITY.md).
