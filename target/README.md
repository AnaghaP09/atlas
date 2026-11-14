# Demo target content

`repo/` is the content of the demo target repository that ATLAS publishes draft pull requests to.
It is a small, team-written Go package (synthetic demo content, not taken from another project).

- `repo/netaddr/` holds the package and its regression tests.
- `repo/.atlas/behaviour/` holds the behavioural tests written before any task exists. They fail at
  the base commit and must pass after a correct fix. Coding-agent tasks may never change this
  directory; the CI workflow copies these tests into the package before running them.
- `repo/.github/workflows/ci.yml` runs the build, vet and every test inside a container with no
  network, no secrets and a read-only checkout, using the same pinned image as the local sandbox.

The demo task: fix host:port formatting for IPv6 addresses in `netaddr.JoinHostPort`.
