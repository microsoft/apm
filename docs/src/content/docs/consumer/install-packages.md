---
title: Add and remove packages
description: Add a package from a repo, tag, subfolder, single file or local bundle, see why it is installed, and remove it cleanly.
---

Use this page to bring a skill, agent or set of instructions from another
repository into your project, at a version the whole team shares. A
**package** is any git repository or folder APM can install, with or without
its own `apm.yml`.

## Add a package

```bash frame="terminal"
apm install microsoft/apm-sample-package#v1.0.0
```

APM adds the entry to `apm.yml`, downloads the package and its dependencies
into `apm_modules/`, and writes files where each harness (the agent tool that
reads them) looks: `.github/` for GitHub Copilot, `.claude/` for Claude Code.
It records the commit and file hashes in `apm.lock.yaml`. A failed install
restores `apm.yml`.

You can also edit the manifest and run a bare `apm install`:

```diff title="apm.yml"
 dependencies:
   apm:
     - microsoft/apm-sample-package#v1.0.0
+    - contoso/review-skills#v1.1.0
```

## Reference formats

`apm install` and `dependencies.apm` accept these forms:

| Form | Example | Use it for |
|---|---|---|
| Shorthand | `contoso/review-skills` | A GitHub repo at its default branch |
| Tag, branch or commit | `contoso/review-skills#v1.1.0`, `#main`, `#<40-char SHA>` | A fixed (tag, SHA) or moving (branch) version |
| Semver range | `contoso/review-skills#^1.1.0` | The highest matching release tag |
| Subfolder | `github/awesome-copilot/skills/review-and-refactor` | One skill or folder from a larger repo |
| Single file | `contoso/standards/prompts/review.prompt.md` | One primitive file |
| Other host | `gitlab.com/acme/standards#v2.0`, `dev.azure.com/org/project/repo` | GitLab, GitHub Enterprise, Azure DevOps |
| Git URL | `https://...git`, `git@host:org/repo.git` | An explicit transport |
| Local path | `./packages/shared-skills`, `./ai/agents/reviewer.agent.md` | A folder, or one `.agent.md`, `.instructions.md` or `.prompt.md` |
| Marketplace | `review-kit@contoso` (CLI) | A plugin listed in a [marketplace](../installing-from-marketplaces/) |

Shorthand resolves against `github.com` unless `apm.yml` sets `default_host:`.
For nested GitLab groups or aliases, use the
[object form](../../reference/manifest-schema/#412-object-form) (`git:`,
`path:`, `ref:`, `alias:`). Private hosts need
[credentials](../../getting-started/authentication/).

### Pin a semver range

A range (`^1.2.0`, `~1.4`, `>=2.0 <3`, `1.5.x`) resolves against the
repository's tags (`v1.2.3`, `review-skills-v1.2.3` or
`review-skills--v1.2.3`) and locks the highest match. Clones replay that tag;
[`apm update`](../update-and-refresh/) or an edit to the range moves it.

## Install one skill from a collection

When a repository ships many skills under `skills/<name>/`, take the ones you
need:

```bash frame="terminal"
apm install contoso/review-skills --skill security-review
```

APM saves the selection as a `skills:` list on the entry, so a bare
`apm install` deploys the same subset. A later `--skill` adds to the list;
`--skill '*'` resets it to every skill. To drop one, edit the `skills:` list
and run `apm install`.

## Install local paths and bundles

A local folder or file installs like any other package. A single file is
copied alone, so use a folder when it needs companion files. Rerun
`apm install` after you edit the source.

A **bundle** is a `.zip` (or legacy `.tar.gz`) or folder that `apm pack`
produced, with `plugin.json` at its root:

```bash frame="terminal"
apm install ./dist/review-skills-1.1.0.zip --target copilot
```

APM checks the files against the hashes `apm pack` recorded in the bundle,
deploys them and records them in `apm.lock.yaml`. It does not edit `apm.yml`. A reinstall
removes bundle MCP and LSP servers that are no longer shipped, are denied or are not
approved. Bundle MCP servers are recorded in the lockfile, so they pass `apm audit --ci`
and `apm install --frozen` and survive other installs and uninstalls; `apm install -g
<bundle>` records them in `$APM_HOME/apm.lock.yaml`. A `bundle:` entry counts only
when the lockfile also records its deployment, and org MCP policy still applies to
it. With `-g`, bundle executable approvals are read from `~/.apm/apm.yml`, never the
current directory's `apm.yml`. To build one, see
[Build a plugin](../../producer/build-a-plugin/).

## Transport selection

APM picks one transport per dependency and does not silently switch:

| Dependency form | Transport |
|---|---|
| `ssh://...` or `user@host:...` | SSH |
| `https://...` or `http://...` | The scheme you wrote |
| Shorthand with `--ssh`, `APM_GIT_PROTOCOL=ssh` or `apm config set prefer-ssh true` | SSH |
| Any other shorthand | HTTPS |

Same-host git `insteadOf` rewrites still apply; rewrites that add
credentials, downgrade to `http://` or change host are refused. Use
`--allow-protocol-fallback` for a migration window, no longer. `http://` sources
need `--allow-insecure`.

## Transitive dependencies and the lockfile

APM installs your dependencies' dependencies too. The lockfile records each
package's commit and a SHA-256 hash per file, so `apm install` on another
machine replays the same bytes, even for branches. APM installs one version
per package: if two dependencies pin different commits of one package, install
fails naming both paths. Align the refs in `apm.yml`; never edit the lockfile.

Ask why a package is on disk:

```bash frame="terminal"
apm deps why github/awesome-copilot/skills/review-and-refactor
```

```text frame="none"
[i] github/awesome-copilot@unknown  (transitive)

    microsoft/apm-sample-package   [declared in apm.yml]
    +-- github/awesome-copilot/skills/review-and-refactor
```

The sample pulls this skill without a tag, so APM shows no version
(`@unknown`); the lockfile still pins its commit. `apm deps list` lists installed
packages and `apm deps tree` shows the graph (`apm list` lists scripts).

MCP servers that come from a transitive package are held back until you
approve them: see [Approve hooks and MCP servers](../approve-executables/).

## What to commit

Commit `apm.yml` and `apm.lock.yaml`. APM adds its download folder,
`apm_modules/`, to `.gitignore`. Committing the deployed harness files is
optional, since any clone rebuilds them from the lockfile
([should I commit them?](../../concepts/how-apm-works/#common-questions)).
Native plugins are the exception: commit `.agents/plugins/`,
`.claude/settings.json` and `.github/copilot/settings.json` so a fresh clone
loads them without APM ([Native plugins](../install-plugins/#commit-native-plugins)).

## Remove a package

```bash frame="terminal"
apm uninstall contoso/review-skills
```

This removes the `apm.yml` entry, its lockfile row and its deployed files.
`review-kit@contoso` works for marketplace entries. After deleting an entry by
hand, run `apm prune` (preview with `--dry-run`) to remove orphaned packages.

## Verify

```bash frame="terminal"
apm audit
```

```text frame="none"
[+] Replayed 2 package(s)
[+] No drift detected
...
```

`apm audit` rebuilds what the lockfile describes and compares it with disk.

## When things go wrong

- **`No harness detected`.** See
  [Choose your harnesses](../choose-harnesses/).
- **401, 403 or "not accessible" on a private repo.** See
  [Use private sources](../../getting-started/authentication/).
- **Critical hidden-Unicode finding.** Install stops and names the file. Fix
  it upstream; `--force` overrides.
- **Policy violation.** Your organization's policy blocked the dependency
  before any file was written. Run `apm policy status`; see
  [Set an org policy](../../enterprise/apm-policy/).
- **One package in a multi-package install fails.** If a download fails, or a local
  path is not a package, APM integrates nothing from that run. Fix the failing entry
  and rerun.
- **A local file is in the way.** APM does not overwrite files it did not
  write. Move it and rerun.
- **`--frozen` fails with `Content hash mismatch`.** See
  [Common errors](../../troubleshooting/common-errors/#content-hash-mismatch-for-pkg-expected-a-got-b).
- **Lockfile written by a newer APM.** Upgrade APM; see
  [Install failures](../../troubleshooting/install-failures/#lockfile-written-by-a-newer-apm).

:::tip[Other options]
`--dev` adds to `devDependencies` (left out of `apm pack`). `--dry-run`
previews. `-g` installs for your user account, not the project
([Choose your harnesses](../choose-harnesses/#install-for-your-user-account)).
Every flag: [`apm install`](../../reference/cli/install/).
:::
