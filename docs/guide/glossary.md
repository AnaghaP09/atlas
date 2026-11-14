# Glossary

## Data labels

Every result has a label. The label says where the data came from.

| Label | Meaning |
|---|---|
| Real data | Real public data and real AI model calls. |
| Sample data | Data that the team made up. ATLAS never counts it as a real result. |
| Not in this demo | The code exists, but the real-world part is not in this build. |
| Not measured | A quality target that this build cannot measure. It is not a pass. |

## Terms

| Term | Meaning |
|---|---|
| Access request | A request to read one item that your role cannot open. See [Access requests](access-requests.md). |
| Approval A | The first approval for a code change. It lets the coding agent prepare the change. |
| Approval B | The second approval. It covers the exact change and lets ATLAS publish it as a draft pull request. |
| Carry-over | An issue that is open at the end of one release and still open at the next. |
| Connected | The mode in which ATLAS may call an AI model service and read public sources. |
| Correction | A change to a published text that removes or fixes a wrong claim. |
| Data as of | The date of the frozen copy of project data that a run or view uses. See [Data as of](data-as-of.md). |
| Example case | A frozen copy of the project at the moment a known past failure was reported. |
| Forecast | The list of debt and issues that the next release is likely to inherit. |
| Incident | The record that ATLAS opens each time a kill switch stops something. |
| Kill switch | A control that stops a part of ATLAS when something unsafe happens. |
| Offline | The mode in which ATLAS refuses every outgoing connection. AI steps then stop. |
| Proxy blocker | A stand-in for a release blocker. See below. |
| Read-only access | Access that lets you read an item but not approve, publish or change anything. It ends after 24 hours. |
| Release close | The run at the end of a release that builds the forecast. |
| Sandbox | An isolated container where the coding agent's change is built and tested. It has no network and no secrets. |
| Spend ceiling | The most that ATLAS may spend on AI model calls. Runs stop when ATLAS reaches it. |
| Test copy | An example case with the names disguised. It checks that ATLAS does not repeat an answer from memory. |

## Why ATLAS uses a proxy blocker

A release blocker is an issue that had to be fixed before a release could ship. ATLAS measures
its forecast by asking: did it name the blockers in advance?

KubeVirt does not record which issues blocked each release. So ATLAS uses a stand-in. An issue
counts as a blocker if one of these is true:

- It is a bug or a flaky-test issue, and its fix was copied to the release branch before the
  release.
- It is a critical bug or flaky-test issue that was still open on the day of the release.

Two reviewers check each one. Every forecast accuracy number says that it uses this stand-in.
