# Security policy

## Report a security problem

Report security problems privately through GitHub:

1. Open the repository's **Security** tab.
2. Choose **Report a vulnerability**. This creates a private security advisory.

Do not open a public issue for a security problem.

In your report, say what you found and how to repeat it. Also say what an attacker could do with
it. We reply through the advisory.

## How ATLAS protects itself

ATLAS is still being built. This list describes the controls that ATLAS is designed and tested
to enforce.

- **Local only.** In this build, the server listens only on local addresses. It refuses to start
  on any other address while the demo role switcher is in use.
- **Host and Origin checks.** The server rejects a request unless its `Host` header is an exact
  local address. It also rejects a browser `Origin` that is not on the allowed list. These checks
  run before any other handling. They stop web pages that try to reach the local server through
  DNS rebinding.
- **Retrieved text is data.** ATLAS never follows instructions that it finds in logs, tickets or
  commit messages. Code outside the AI model controls which tools an agent may use.
- **Removal of sensitive data.** All data sent to an AI model passes one point. That point
  removes secrets, server names and personal data. If it cannot classify part of a message, it
  does not send the message.
- **Isolated code changes.** ATLAS builds and tests a coding agent's change inside a container.
  The container has no network, a read-only file system, no secrets and no administrator rights.
- **Approvals.** Every code change needs two approvals. Each approval is tied to the exact
  change, the target repository and the security decision. Approvals expire. ATLAS checks them
  again before every write.
- **Containment.** A safety violation trips a switch. The switch stops AI calls, change
  preparation or publishing until the conditions for that incident are met.
- **No secrets in the repository.** Settings live in a `.env` file that Git ignores. The
  dependency files list exact versions with checksums, and ATLAS audits them for known flaws. A
  check for secrets and personal data runs before every push.
