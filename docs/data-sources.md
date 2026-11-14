# Data sources

ATLAS works with any software project. The demo and the tests use public data from
[KubeVirt](https://github.com/kubevirt/kubevirt). KubeVirt is an open-source project that runs
virtual machines on Kubernetes. It has a long history of issues, pull requests and test runs.

| Source | What ATLAS reads | How | Label |
|---|---|---|---|
| Issue tracker | Issues, comments, labels, timeline events and edit history of `kubevirt/kubevirt` | GitHub GraphQL and REST interfaces, read only | REAL |
| Git | Commits, pull requests, changes, release tags and release branches | GitHub REST interface, read only | REAL |
| Test system (CI) | Test job logs that issues link to. CI means continuous integration: the system that builds and tests every change. KubeVirt's CI system is called Prow. | Public storage, read only | REAL |
| Security scanner | Known security flaws in the Go modules listed in `go.mod` at one fixed commit | The [OSV.dev](https://osv.dev) query interface | REAL |
| System map | Components, owning teams and the links between components. ATLAS reads them from Go import statements and OWNERS files. | Built from the repository at one fixed commit | REAL (derived) |
| Kubernetes events | Cluster events for failures caused by the environment | Data that the team made up | SYNTHETIC |
| Error rates | Totals of errors over time | Data that the team made up | SYNTHETIC |
| Staffing | Role ids, skill tags and availability. Never names. | Data that the team made up | SYNTHETIC |

## Rules

- **Past cases show only what was known then.** ATLAS rebuilds each past case as it stood at a
  cut-off time. This includes text that people edited later: ATLAS rebuilds the earlier text
  from the edit history. Anything that appeared after the cut-off, such as the fix or a closing
  comment, is hidden from the agents.
- **No personal data in the repository.** Saved test data replaces names, user handles and email
  addresses with fixed stand-in names. It keeps the source links. Raw downloads stay on the local
  computer. They are never committed.
- **Old data is not used as current.** Each source has a maximum age. ATLAS reports evidence
  that is too old, or has no time stamp, as unknown or unavailable.
- **Blocking issues use a stated substitute.** KubeVirt does not record which issues blocked
  each release. So when ATLAS scores its forecasts against blocking issues, it uses this
  substitute definition. A blocking issue is a bug or flaky-test issue whose fix was copied to
  the release branch before the release. A critical issue that was still open on the day of the
  release also counts. Two reviewers check each candidate. Every result shows this limit next to
  the number.

ATLAS always labels made-up data. It never counts made-up data as a real result.
