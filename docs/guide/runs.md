# Runs

## What this page is for

This page lists every run with its status and its reports. A run is one piece of ATLAS work, such as one investigation.

## Who uses it

Product manager, engineering manager, solution architect, on-call engineer and audit.

## How to use it

1. Find the run in the list.
2. Select it to see Scout's steps, Sentinel's checks and the limits used.
3. Open a report from the run.

## Run states

| State | Meaning |
|---|---|
| Complete | The run finished and Sentinel checked it. |
| Partial | The run reached a time or step limit. The report says what is missing. |
| Unverified | Sentinel could not finish. Nothing was published. A person must look. |
| Failed | The run stopped with an error. A notification says why. |

## Good to know

- A run shows its own AI model spend. Older runs show **Model spend not recorded**, because their
  spend was not counted per run.
- After a run ends, the Progress box shows no steps. Steps are shown while the run is in progress.
