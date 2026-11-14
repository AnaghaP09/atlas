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
  scores its forecasts against release blockers, it uses a stated proxy definition (a bug or
  flaky-test issue whose fix was backported to the release branch before the release, or a
  critical issue still open at the release tag), adjudicated by two reviewers, and every result
  shows that limitation beside the number.

Synthetic data is always labelled and is never scored as observed.
