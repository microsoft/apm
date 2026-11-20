---
title: Install from a marketplace
description: Register a marketplace, find a plugin by name, install it into the lockfile, and know what replays without the marketplace.
---

Use this page when someone tells you "install `review-kit` from the Contoso
marketplace". A **marketplace** is a published list of plugins and packages:
a `marketplace.json` file in a git repository or at an HTTPS URL. Each entry
names a plugin and the repository and version it comes from. Organizations run
private marketplaces to offer a curated set of approved agent capabilities;
see [Build your organization's marketplace](../../enterprise/org-marketplace/).

APM installs marketplace plugins into the same manifest and lockfile as any
other package, so teammates and CI get the same files.

## Add a marketplace

Register it once per machine, pinned to a tag:

```bash frame="terminal"
apm marketplace add contoso/agent-marketplace#v2.0.0 --name contoso
```

Without `#ref` (or `--ref`), APM tracks the default branch and warns that it
can move. `SOURCE` can also be a GitLab, Azure DevOps or other git URL, an SSH
URL, a local folder, or a hosted `marketplace.json` URL. APM downloads
hosted URLs over HTTPS with no credentials, so use a git-backed marketplace
for private lists ([credentials](../../getting-started/authentication/#generic-marketplace-git-transport)).
If the file is not in a default location, pass `--path`.

`apm marketplace list` shows what you registered; `apm marketplace remove
contoso` forgets it.

## Find a plugin

```bash frame="terminal"
apm marketplace browse contoso
apm search review@contoso
```

`browse` lists every entry; `search` takes `QUERY@MARKETPLACE` and matches
names, descriptions and tags. Each row prints the install name, such as
`review-kit@contoso`.

## Install patterns

Through APM:

```bash frame="terminal"
apm install review-kit@contoso
```

APM looks up `review-kit`, installs it from the source and ref the marketplace
lists, and records the plugin in `apm.lock.yaml`. Append `#v2.0.0` to install
a specific tag instead of the listed one. To follow a version range, declare
the entry in `apm.yml`:

```diff title="apm.yml"
 dependencies:
   apm:
+    - name: review-kit
+      marketplace: contoso
+      version: "^2.0.0"
```

APM picks the highest release tag that matches, using the tag pattern the
publisher declared, and fails if none matches.

The same marketplace often works in each harness's own commands too (a
harness is the agent tool that reads the files, such as Claude Code). Those
install the plugin for one user, with no project lockfile, policy check or
`apm audit`:

| Tool | Install command |
|---|---|
| APM (project lockfile) | `apm marketplace add OWNER/REPO` then `apm install NAME@MARKETPLACE` |
| GitHub Copilot CLI | `copilot plugin marketplace add OWNER/REPO` then `copilot plugin install NAME@MARKETPLACE` |
| Claude Code | `/plugin marketplace add OWNER/REPO` then `/plugin install NAME@MARKETPLACE` |
| Codex CLI 0.163.0+ | `codex plugin marketplace add ...` then `codex plugin add NAME@MARKETPLACE` |

Use the native command for a personal tool. Use `apm install` when the plugin
belongs to the project.


A Claude Code plugin installed with `apm install <plugin>@<marketplace>` for
`claude` or `copilot` is deployed whole to `.agents/plugins/` and declared in
the harness settings; see [Native plugins](../install-plugins/#native-plugins).

## How marketplace installs lock

The lockfile records the plugin's commit and file hashes like any package,
plus where it was found:

```yaml title="apm.lock.yaml"
- repo_url: contoso/review-kit
  resolved_commit: 8c1f0b2...
  discovered_via: contoso
  marketplace_plugin_name: review-kit
  source_url: https://github.com/contoso/agent-marketplace
  source_digest: sha256:...
```

- `apm install` and `apm install --frozen` replay the locked commit without
  contacting the marketplace, so an edit to `marketplace.json` changes nothing
  until you update. A package whose locked MCP or LSP servers have no copy in
  `apm_modules/` still reads the marketplace for those definitions only; the
  locked commit still pins the download. If the marketplace now selects another
  source or commit, install fails; run `apm update <plugin>@<marketplace>` to
  move to it.
- `apm update` and `apm install --refresh` revalidate the marketplace and fail
  rather than use stale data. `apm marketplace update` refreshes the cached
  list and nothing else.
- The name `contoso` is local to your machine. If it later points at a
  different marketplace, `apm update`, `apm install <plugin>@contoso` (with or
  without `-g`), `apm outdated` and any other re-resolution fail and name both
  origins instead of switching.

`source_url` records the origin; the digest records provenance and is not a
trust anchor.

## Declare marketplaces in your user manifest

To register the same marketplaces on every machine, list them in your user
manifest (`~/.apm/apm.yml`, or `$APM_HOME/apm.yml`) and run `apm install -g`:

```yaml title="~/.apm/apm.yml"
marketplaces:
  - name: contoso
    source: https://github.com/contoso/agent-marketplace
    ref: v2.0.0
```

APM adds missing entries, updates changed ones, and removes ones it added that
you deleted. It leaves registrations you made with `apm marketplace add`
alone. A project `apm.yml` cannot declare `marketplaces:`. Registering grants
no trust and installs nothing.

## Codex compatibility bundles through APM

A plugin that ships `.codex-plugin/plugin.json` can be registered with Codex
as a whole bundle:

```bash frame="terminal"
apm install -g contoso/review-kit --target codex
```

This needs Codex CLI 0.163.0 or later on `PATH` and works at user scope
(`-g`); project scope fails. Approve the package's executables before you
install ([Approve hooks and MCP servers](../approve-executables/)). APM scans and
copies the complete bundle, registers it through Codex's configuration
interface disabled, and never runs plugin code or grants Codex trust. Enable
it in Codex. APM keeps its ownership journal in `$APM_HOME/.codex-native/`;
keep that folder. It was qualified on macOS arm64. See
[Install plugins](../install-plugins/) for current limits.

## Verify

```bash frame="terminal"
apm deps list
```

The plugin appears with its primitive counts. Commit `apm.yml` and
`apm.lock.yaml`.

## When things go wrong

- **`marketplace.json` changed but my install did not.** Expected: run
  `apm outdated` to see it, then `apm update`.
- **Refused bare cross-repo source on `*.ghe.com`.** The entry would fall back
  to public `github.com`, so APM refuses it as a dependency-confusion risk.
  Ask the publisher to host-qualify the entry. A bare `owner/repo` is accepted
  when your `GITHUB_HOST` is the marketplace host, because it then resolves
  to that host.
- **The plugin installs but is incomplete in Claude Code.** See
  [Install plugins](../install-plugins/#known-gaps).

:::tip[Other options]
Publish your own: [Publish to a marketplace](../../producer/publish-to-a-marketplace/).
Every flag: [`apm marketplace`](../../reference/cli/marketplace/).
:::
