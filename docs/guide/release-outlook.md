# Release outlook and ship readiness

## What this is for

The release outlook answers two questions on every page:

1. **What will the next release inherit, and who should work on it?** These are the technical
   debt items and open issues that ATLAS expects to carry over from this release. For each item,
   ATLAS suggests developers, the coding agent, or "decide first".
2. **What will stop us shipping, which services does it touch, and can we still deliver?** This
   is the state of the release right now.

The outlook is a strip under the top bar. The **Ship readiness** page shows the second question
in full. Your home page starts with both questions.

## Who uses it

The product manager, the engineering manager and the solution architect. The pages for the
security engineer, the on-call engineer and the audit role are in beta and do not show the
outlook yet.

## How to read the strip

- **Next release inherits 8 items.** The number of items in the latest checked release close.
  Select it to see the list.
- **Devs, Agents, Decide first.** How many items ATLAS suggests for developers, for the coding
  agent, and for a person to decide first. Select a number to see those items.
- **Predicted** and a date. The items are a prediction from a frozen copy of the project data.
  The date is when that copy was taken. See [Data as of](data-as-of.md).
- **Ship status.** One of On track, At risk, Blocked or Not measured. Select it to open
  Ship readiness.
- **Blockers** and **services.** How many things stop or risk the release, and how many
  services they touch. Select them for the list.

The strip updates on its own when its inputs change.

## Where the answers come from

### The next release (first question)

ATLAS reads the newest release close that finished and passed its checks. It does not mix
runs. If no release close has finished yet, the strip says **No release close yet**. People who
may start one see a **Run** button.

Each item shows a short reason for the suggested owner and a link to its evidence. ATLAS
suggests an owner from the kind of work:

- **Coding agent:** a small change in one component with a clear cause. Examples: one flaky
  test, a dependency update, a small fix that a failing test shows.
- **Developers:** work across several components, work that needs special hardware, work where
  many tests fail and the cause is not clear, or a large change.
- **Decide first:** a real decision only. Examples: the item is marked as a release blocker, or
  it needs a policy or security exception.

A priority label raises an item's score. It does not decide the owner on its own.

Scores are illustrative. ATLAS never sets dates and never adds a confidence number.

### Shipping now (second question)

ATLAS checks four inputs:

| Input | Where it comes from |
|---|---|
| Failing CI jobs | The live dashboard |
| Security gate | The latest dependency scan on the live dashboard |
| Investigations waiting for a person | Finished investigations with no recorded decision |
| Open critical issues | Open issues with the project's critical label |

## The ship status rules

ATLAS applies these rules in order. The page shows every rule that fired.

1. **Blocked** if the security gate is blocked (the scan found at least one blocking finding),
   or an investigation asks for a release decision and nobody has recorded one.
2. **At risk** if 3 or more CI jobs are failing, at least 1 critical issue is open, or an
   investigation is held for a person.
3. **Not measured** if no rule fired and at least one input is missing. ATLAS does not call a
   release on track when it could not check everything.
4. **On track** if every input was checked and no rule fired.

Recording a decision on the [Record a decision](record-a-decision.md) page clears the
investigation from the rules.

## Services

Each blocker is linked to a service where ATLAS can tell which one:

- a CI job by the `sig-` name in the job name;
- an issue by its `sig/` label;
- a dependency finding by **project-dependencies**.

Items without a match are listed as **not mapped**. The solution architect can open the
[Knowledge graph](knowledge-graph.md) from each service.

## Good to know

- Security finding details stay with the security engineer. The outlook shows counts only.
- Some reports open only for their own role. The outlook then links to the run instead.
