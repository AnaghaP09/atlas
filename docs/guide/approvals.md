# Approvals

## What this page is for

Every code change needs two approvals from an engineering manager. Approval A lets the coding agent prepare a change. Approval B publishes the exact change shown.

## Who uses it

Engineering manager and audit.

## How to use it

1. Open the request.
2. For Approval A, read the task, the files it may touch and the target repository.
3. For Approval B, read the exact change, line by line.
4. Approve or reject.

## Good to know

- Each approval ends after 48 hours.
- ATLAS checks both approvals again before every write.
- If the security check is too old, refresh it. Both approvals then start again.
- Approval B needs the sandbox. Without Docker, a change can be prepared but not published.
