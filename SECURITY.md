# Security policy

## Reporting a vulnerability

Please report security problems privately through GitHub: open the repository's **Security**
tab and choose **Report a vulnerability** to create a private security advisory. Do not open a
public issue for a vulnerability.

Include what you found, how to reproduce it, and what an attacker could do with it. We will
reply through the advisory.

## Security model

ATLAS is still being built; these are the controls it is designed and tested to enforce.

- **Local only.** In this build the server listens only on loopback addresses and refuses to
  start on any other address while the demo role switcher is the identity.
- **Host and Origin checks.** Requests whose `Host` header is not an exact loopback value, or
  whose browser `Origin` is not on the allow-list, are rejected before routing. This blocks web
  pages that try to reach the local server through DNS rebinding.
- **Retrieved text is data.** Instructions found in logs, tickets or commit messages are never
  followed; tool allow-lists are enforced in code, outside the model.
- **Redaction.** One outbound boundary removes secrets, host names and personal data before any
  model call and withholds a payload it cannot classify.
- **Sandboxed execution.** The coding agent's changes are built and tested in a container with
  no network, a read-only file system, no secrets and a non-root user.
- **Approvals.** Every code change needs two approvals bound to the exact diff, the destination
  and the security decision; both expire and are re-checked before every write.
- **Containment.** Safety violations trip switches that stop model calls, preparation or
  publication until incident-specific conditions are met.
- **No secrets in the repository.** Configuration lives in a git-ignored `.env` file.
  Dependencies are locked with hashes and audited, and a check for secrets and personal data
  runs before every push.
