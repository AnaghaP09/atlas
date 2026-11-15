# Quality checks

## What this page is for

This page shows the quality targets that ATLAS must meet, and the latest result for each.

## Who uses it

Product manager and audit.

## How to use it

1. Read each line. It shows passed, failed or not measured.
2. Open a line to see the numbers.

## Good to know

- A target that is not measured is never shown as passed.
- The quality checks run on KubeVirt public data only.
- The page lists **Test copies**. These are example cases with the names disguised. See [Data as of](data-as-of.md).
- Forecast accuracy uses a stand-in for release blockers. See *proxy blocker* in the [glossary](glossary.md).
- Forecast accuracy counts only blockers that were already open when the forecast was made. The
  forecast cannot name an issue that nobody had reported yet, so those issues are left out of the
  score. They are still kept, with the reason, for anyone who wants to check.
- Each forecast accuracy number has this note beside it: "Counts only blockers already open when
  the forecast was made; uses a substitute for release blockers because KubeVirt does not record
  them."
- Forecast accuracy shows one pooled number and one number for each release. If the pooled number
  has nothing to count, the check fails. A release with nothing to count shows "no denominator".
