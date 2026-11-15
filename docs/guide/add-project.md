# Add project

## What this page is for

This page adds a public GitHub repository to ATLAS as a new project. It then builds frozen
**Data as of** copies of that project, so you can run a release close on it.

## Who uses it

The audit role. Other roles do not see this page.

## How to use it

1. Open **Add project**.
2. Paste the repository link, for example `https://github.com/owner/name`. You can also type
   `owner/name`.
3. Select **Detect**. ATLAS reads the repository's public facts. This takes about ten requests
   to GitHub.
4. Check each value in the table. Change a value if it is wrong.
5. Select **Add project**. The project appears in the project switcher in the top bar.
6. Select **Switch to** the new project. The live dashboard starts to fill within a minute.
7. On the same page, choose a release tag and select **Build copy**. ATLAS builds a frozen copy
   of the project as it stood on the day of that tag.
8. When the copy is ready, select **Run release close**. The run uses the copy you built.

## What the labels mean

Each value in the table has one label.

| Label | Meaning |
|---|---|
| detected | ATLAS found the value in the repository. The Evidence column says where. |
| needs you | ATLAS could not decide. Fill the value, or leave it empty on purpose. |

## What ATLAS detects

| Value | How ATLAS finds it |
|---|---|
| Project id and name | From the repository name. |
| Release tags | From the published releases. Pre-releases do not count. |
| CI | GitHub Actions when the repository has workflow files. Prow is set up by hand. |
| Languages | From GitHub's language count. |
| Bug and flaky-test labels | From the label names. For example `bug`, `kind/bug` and `type: bug` all mean a bug. ATLAS also counts how many open issues carry the label today. |
| Owner files | `OWNERS` and `CODEOWNERS` files. Only teams count as owners, never single people. |
| Dependency list | `go.mod`, `package-lock.json`, `uv.lock`, `poetry.lock` or `requirements.txt`. |

If a bug label exists but no open issue carries it, ATLAS marks it **needs you**. Clear the
value to make ATLAS read every open issue instead.

## What ATLAS never does

- ATLAS never gives a new project a publish target. A project cannot publish changes until a
  person adds a test repository by hand in the projects file.
- ATLAS never writes to the repository. Every read is a GET request through the permission
  gateway.
- The automatic runs that use the AI model start **paused** for the new project. Turn them on
  in [Automatic runs](automatic-runs.md) if you want them.

## Build a Data as of copy

A copy holds the project as it stood when the release tag was made:

- the bug and flaky-test issues that were open on that day, with their labels, title and
  comments as they were then;
- the newest commits up to the tag;
- a map of the code's components and the links between them;
- a dependency scan that counts only security advisories published before that day;
- made-up team capacity, marked as such.

ATLAS replaces people's names with stable codes, such as `person-1a2b3c4d`. ATLAS refuses the
copy if any part of it is dated after the tag.

The copy gets a readable name, for example **Example at release v2.0.0 · 3 Mar 2026 · 41 open
issues**. You find it in the data list on [Run now](run-now.md) under **Releases**.

ATLAS builds one copy at a time. The page shows each step and the number of GitHub requests
used.

## Limits

| Limit | Why |
|---|---|
| At most 1,500 GitHub requests and 400 issues for one copy | Keeps one copy from using up the hourly request allowance. |
| Without a GitHub token, only small repositories (20 issues or fewer) | GitHub then allows 60 requests an hour, and the issue history needs a token. Set `GITHUB_TOKEN` in `.env` for larger repositories. |
| Issues are found by their labels today | An issue whose label was removed after the tag is not in the copy. |
| Import links only for Go, Python, JavaScript and TypeScript | Other languages show folders only. The map says so. |

## Start with a link already filled in

Set `ATLAS_ADD_PROJECT` in `.env` to a repository link. The page then opens with that link in
the box. You still review and confirm every value.

## Good to know

- With sign-in turned on, a new project has no members yet. Add people to it in the users file.
- The project is written to the projects file at once, so it stays after a restart.
- See [Data as of](data-as-of.md) for why ATLAS works from frozen copies.
