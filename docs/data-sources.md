# Data sources

ATLAS works with any software project. A project is a set of read-only sources that you define
in a projects file. The example file defines two public open-source projects:

| Project | What it is | How ATLAS uses it |
|---|---|---|
| [KubeVirt](https://github.com/kubevirt/kubevirt) | Runs virtual machines on Kubernetes. It has a long history of issues, pull requests and test runs. | Frozen data copies for forecasts and investigations, the quality tests, and a live dashboard. |
| [Argo CD](https://github.com/argoproj/argo-cd) | Deploys applications to Kubernetes. | A live dashboard only. It shows that ATLAS is not tied to one project. |

## What ATLAS reads

| Source | What ATLAS reads | How | Kind of data |
|---|---|---|---|
| Issue tracker | Issues, comments, labels, timeline events and edit history | GitHub, read only | Real data |
| Git | Commits, pull requests, changes, release tags and release branches | GitHub, read only | Real data |
| Test system (CI) | For KubeVirt, the test job logs that issues link to. For Argo CD, recent GitHub Actions runs. CI means continuous integration: the system that builds and tests every change. | Public storage and GitHub, read only | Real data |
| Security scanner | Known security flaws in the Go modules that a project lists | The [OSV.dev](https://osv.dev) query service | Real data |
| System map | Components, owning teams and the links between components, read from Go import statements and OWNERS files | Built from the repository at one fixed commit | Real data (derived) |
| Kubernetes events | Cluster events for failures caused by the environment | Made up for the demo | Sample data |
| Error rates | Totals of errors over time | Made up for the demo | Sample data |
| Staffing | Role ids, skill tags and availability. Never names. | Made up for the demo | Sample data |

ATLAS always labels sample data. It never counts sample data as a real result.

Two gaps remain in this build:

- The live dashboard does not list KubeVirt's recent test runs. It says so on the panel.
- The frozen data copies hold no security scans. A security check on them reports "not run".

## Frozen data copies

The repository includes 24 frozen copies of KubeVirt data. The interface shows each one as
**Data as of** a date.

| Group | Count | What each one holds |
|---|---|---|
| Releases | 5 | KubeVirt on the day of each release from v1.5.0 to v1.9.0, with its open bug and flaky-test issues. A flaky test is one that sometimes fails without a code change. |
| Example cases | 7 | One real failure each, as it stood when it was reported. |
| Test copies | 12 | A copy of each of the above with names and numbers disguised. The quality tests use them to check that ATLAS is not reciting from memory. |

## Rules

- **Past cases show only what was known then.** ATLAS rebuilds each past case as it stood at a
  cut-off time. This includes text that people edited later: ATLAS rebuilds the earlier text
  from the edit history. Anything that appeared after the cut-off, such as the fix or a closing
  comment, is hidden from the agents.
- **No personal data in the repository.** Before data is saved here, a cleaning step replaces
  each person's name and user handle with a fixed stand-in, such as `person-3fa9c1d2`. It
  removes email addresses, embedded images, keys and access tokens in links. A check then scans
  the cleaned files for anything that survived. Raw downloads stay on the local computer. They
  are never committed.
- **Old data is not used as current.** Each source has a maximum age. ATLAS reports evidence
  that is too old, or has no time stamp, as unknown or unavailable.
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

## Live data and GitHub limits

The live dashboard checks each project at a fixed interval and shows the time of the last check.
The data is as recent as the last check, so it is close to real time but not instant.

| | With a GitHub token | Without a token |
|---|---|---|
| GitHub's limit | 5,000 requests an hour | 60 requests an hour |
| Default interval | 60 seconds | 600 seconds |
| What is stored for each issue | The issue with its edit history | The issue as GitHub shows it now, marked "edit history unknown" |

ATLAS asks GitHub only for what changed since the last check. When few requests remain, it waits
until GitHub's limit resets. Offline mode pauses the checks.

A read-only token for public repositories is enough. Records marked "edit history unknown" stay
in the live store. ATLAS never uses them for a frozen data copy or a quality test.

## Blocking issues use a stated substitute

KubeVirt does not record which issues blocked each release. So when ATLAS scores its forecasts
against blocking issues, it uses a substitute definition:

- a bug or flaky-test issue whose fix was copied to the release branch before the release; or
- a critical bug or flaky-test issue that was still open on the day of the release.

A person checks each candidate. Every result shows this limit next to the number. This scoring
has not been run yet, so ATLAS claims no forecast accuracy.
