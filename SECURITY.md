# Security policy

## Report a security problem

Report security problems privately through GitHub:

1. Open the repository's **Security** tab.
2. Choose **Report a vulnerability**. This creates a private security advisory.

Do not open a public issue for a security problem.

In your report, say what you found and how to repeat it. Also say what an attacker could do with
it. We reply through the advisory.

## How ATLAS protects itself

ATLAS is not finished. This list describes the controls that exist in the code today. Automated
tests cover each one.

- **Local only.** The server listens only on local addresses. It refuses to start on any other
  address while the role switcher is in use.
- **Host and Origin checks.** The server rejects a request unless its `Host` header is an exact
  allowed address. It also rejects a browser `Origin` that is not on the allowed list. These
  checks run before any other handling. They stop web pages that try to reach the local server
  through DNS rebinding.
- **Sign-in and sessions.** With built-in sign-in, ATLAS stores only a slow one-way hash of each
  password. Five wrong passwords in 15 minutes block that user. The session cookie is not
  readable by scripts and is sent only over HTTPS. A session ends 8 hours after sign-in, or
  after 30 minutes without use. Every change needs a token that proves the request came from
  the ATLAS page. Signing out closes every live update of that session.
- **Roles and projects.** Each role sees only its own pages and data. Before any action, the
  server checks the caller's role in the project that owns the object. A request for another
  project's object gets the same answer as a missing object.
- **Access requests.** A grant to see a locked item is read-only, covers one item and expires.
  It never allows a change. Security findings and audit pages need a decision from the security
  or audit role.
- **Retrieved text is data.** ATLAS never follows instructions that it finds in logs, tickets or
  commit messages. A record that tries to instruct the AI is set aside, and no agent reads it.
  Code outside the AI model controls which tools an agent may use.
- **Removal of sensitive data.** All data sent to an AI model passes one point. That point
  removes secrets, server names and personal data. If it cannot classify part of a message, it
  does not send the message.
- **Isolated code changes.** ATLAS builds and tests a coding agent's change inside a container.
  The container has no network, a read-only file system, no secrets and no administrator rights.
  ATLAS never downloads a container image while it runs. It uses images that you download once
  and that it checks before use.
- **Approvals.** Every code change needs two approvals. Each approval is tied to the exact
  change, the target repository and the security decision. Approvals expire. ATLAS checks them
  again before every write.
- **One place to publish.** ATLAS writes only to the repository named in its settings. It
  refuses a repository that it reads from. Publishing is not connected in this build.
- **Kill switches.** A safety violation trips a switch. The switch stops AI calls, change
  preparation or publishing until the steps for that incident are done. The audit role can run
  a drill on any switch.
- **Spend ceiling.** Runs stop when model spending reaches the limit in the settings.
- **No secrets in the repository.** Settings live in a `.env` file that Git ignores. The
  dependency files list exact versions with checksums, and ATLAS audits them for known flaws. A
  check for secrets, personal data and internal notes runs before every push and on every
  change in GitHub.

## Known limits

- The role switcher gives no real protection. Use it only on your own computer.
- The built-in sign-in is for a demo on one computer. It is not company single sign-on.
- The count of wrong passwords resets when the server restarts.
- No outside party has reviewed this code for security.
