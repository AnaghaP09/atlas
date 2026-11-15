# Data sources

ATLAS is domain- and product-agnostic. Its reference ecosystem for demonstrations and tests is
[KubeVirt](https://github.com/kubevirt/kubevirt), a public open-source virtualisation project with
a long issue, pull-request and CI history.

| Source | What is used | Access | Data label |
|---|---|---|---|
| Issue tracker | Issues, comments, labels, timeline events and edit history of `kubevirt/kubevirt` | GitHub GraphQL and REST, read-only | REAL |
| Git | Commits, pull requests, diffs, release tags and release branches | GitHub REST, read-only | REAL |
| CI | Prow job logs linked from issues | Public storage, read-only | REAL |
| Security scanner | Known vulnerabilities for the Go modules in `go.mod` at a pinned commit | [OSV.dev](https://osv.dev) query API | REAL |
| Architecture graph | Components, owner teams and dependency edges parsed from Go imports and OWNERS files | Derived from the repository at a pinned commit | REAL (derived) |
| Kubernetes events | Cluster events for environment-failure cases | Team-built fixtures | SYNTHETIC |
| Telemetry | Aggregate error-rate series | Team-built fixtures | SYNTHETIC |
| Capacity | Role ids, skill tags and availability; never names | Team-built fixtures | SYNTHETIC |

## Rules

- **As-of snapshots.** Historical cases are rebuilt as they stood at their cut-off time, including
  edited text reconstructed from its edit history. Anything that appeared later, such as the fix
  or a closing comment, is never visible to the agents.
- **Personal data.** Committed fixtures replace person names, handles and emails with stable
  pseudonyms; source URLs and provenance are kept. Raw fetches stay local and are never committed.
- **Freshness.** Each source has a maximum age; stale or untimestamped evidence is reported as
  unknown or unavailable, never used as a current value.
- **Release blockers.** KubeVirt does not record which issues blocked each release. Where ATLAS
  scores its forecasts against release blockers, it uses a stated substitute definition: a bug or
  flaky-test issue whose fix was backported to the release branch between the first beta and the
  release, or a critical-urgent issue still open at the release tag. One person checks each
  candidate.
- **Only blockers that existed at forecast time.** The forecast for a release is made at the close
  of the release before it, from that close's frozen data set. An issue reported after that point
  could not have been named, so counting it would measure timing, not forecasting. A candidate
  therefore counts only if the issue was open in that data set, as it stood at the cutoff. For the
  four scored releases this keeps 36 of 120 candidates; the other 84 are kept in the evaluator
  files with the reason they were left out (most were reported after the forecast). Recall is
  measured against these 36 only. Precision is unchanged: the share of named issues that turned out
  to be blockers. Every result states, beside the number, that it counts only blockers already open
  when the forecast was made and uses a substitute because KubeVirt does not record release
  blockers.

Synthetic data is always labelled and is never scored as observed.
