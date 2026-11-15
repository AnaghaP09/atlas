# Guided demo

## What this page is for

The guided demo walks through each part of ATLAS, one step at a time. Each step is a card.

## Who uses it

Audit.

## How to use it

1. Read the progress line at the top. It says how many steps have passed.
2. Select **Run this step** on a card.
3. Read the result on the card: **Passed** or **Needs attention**.
4. If a step needs attention, the card says why in one line. Select **Details** to see each call.
5. Select **Run again** to repeat a step.

## What the cards show

| On the card | Meaning |
|---|---|
| Data label | The kind of data the step uses. See the [glossary](glossary.md). |
| About $0.10 | The AI model cost that the step is expected to add. Steps with no amount cost nothing. |
| Run step 1 first | The step needs a value from an earlier step. Run that step, then try again. |
| Needs the safety test server | The step runs on the second demo server. Start that server to run it. |

## Good to know

- The second server is the safety test server. It has its own data. It can create test faults, so
  the kill switches can be shown safely.
- A step that needs attention does not block the other steps.
- The forecast accuracy step needs a finished quality check run. Without one it says so.
