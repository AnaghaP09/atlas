# Data as of

## What it means

ATLAS works from a frozen copy of a project's data. The copy shows the project as it stood at
one moment. The copy never changes.

ATLAS shows which copy you are looking at with the words **Data as of**, followed by a date.

Example: **Data as of 30 July 2026** for KubeVirt is the project on the day release v1.9.0
shipped. It holds the issues, code changes and test results that existed on that day. It holds
nothing that happened later.

## Why ATLAS does this

| Reason | What it gives you |
|---|---|
| Fair forecasts | A forecast for the next release must not see the future. A frozen copy cannot contain what happened later, so ATLAS cannot use it. |
| Repeatable reports | A report always points at the same evidence. If you open the report next month, the cited records are the same. |

## The three groups

When you choose the data for a run or a view, ATLAS lists the copies in three groups.

| Group | What it holds | Use it for |
|---|---|---|
| Releases | The project on the day each release shipped. | A release close and its forecast. |
| Example cases | The project at the moment a known past failure was reported. | Investigations of past failures, and the quality checks. |
| Live current data | The project as ATLAS last read it from the public sources. | Seeing what is happening now. |

## Test copies

The [Quality checks](quality-checks.md) page also lists **Test copies**. A test copy is an
example case with the names disguised. Project names, issue numbers and test names are replaced
with made-up ones.

Test copies have one purpose. They check that ATLAS reasons from the evidence and does not
repeat an answer from memory. You do not use them for your own work.

## Good to know

- A run reads only its own copy of the data. It cannot reach another copy.
- Live current data changes each time ATLAS reads the sources. To keep a moment, ATLAS freezes
  it into a new copy with its own date.
