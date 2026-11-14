# System and mode

## What this page is for

This page shows whether ATLAS is healthy, which mode it is in, and how much it has spent on AI
model calls. It also lists open incidents and notifications that could not be delivered.

## Who uses it

Audit.

## How to use it

1. Check the health lines. Each line is one part of ATLAS.
2. Check the mode: Connected or Offline.
3. Check the spend against the spend ceiling.
4. Open an incident to see what it needs. You resume on the [Kill switches](kill-switches.md) page.
5. Read the failed deliveries. Each one says why it failed.

## Connected and Offline

| Mode | What ATLAS may do |
|---|---|
| Connected | Call the AI model service and read the project's public sources. |
| Offline | Make no outgoing connections. Steps that need the AI model stop and say why. |

To change the mode, select the other mode and confirm. In Offline mode, the live dashboard and
the watchers that need the internet pause.

## Spend

The spend ceiling is the most that ATLAS may spend on AI model calls. ATLAS warns at 80 percent.
At 100 percent, runs stop. You set the ceiling in the `.env` file.

## Good to know

- The page never shows a secret. It shows only whether each secret is set.
- An AI model that runs inside your own network is not in this build. Offline mode shows the
  rule that nothing leaves the machine.
