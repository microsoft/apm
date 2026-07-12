---
title: Repo shapes for marketplace producers
description: Two primitive producer postures -- single-plugin and aggregator -- plus the hybrid composition that runs both in one repo, and the noun-verb commands that scaffold each.
sidebar:
  order: 5
---

A marketplace producer repo is just an `apm.yml` (or several) plus a
`marketplace:` block. There is no `--shape` flag and no scaffold
mode: every layout below emerges from the same two commands,
`apm plugin init` and `apm marketplace init`, composed differently.

Two primitive postures cover most repos -- you ship your own plugin,
or you curate others' plugins into a marketplace. The third row in
the table is what happens when one repo does both at once. Pick the
shape that matches how the source code is already organised; you can
migrate later by moving directories and re-running the same commands.

| Shape            | Source files                                      | When                                                |
|------------------|---------------------------------------------------|-----------------------------------------------------|
| Single-plugin    | One `apm.yml` at the repo root                    | One plugin per repo. Smallest surface, fewest gotchas. |
| Aggregator       | One `apm.yml` at the root, N remote `packages:`   | You curate other repos into a marketplace.          |
| Monorepo-hybrid (advanced) | Root `apm.yml` plus per-plugin `apm.yml` subdirs | Many plugins live alongside the marketplace in one repo. Composition of the two postures above. |

When the layout is ready, ship it with the recipe in
[Releasing from any CI](../releasing-from-any-ci/).

## Single-plugin

One repo, one plugin, one marketplace entry pointing at the local
source. The marketplace artifact and the plugin live side by side.

Scaffold:

```bash
apm plugin init my-plugin --yes
apm marketplace init --owner acme-org --name my-marketplace
apm marketplace package add ./ --name my-plugin --version 0.1.0
```

Resulting `apm.yml`:

```yaml
name: my-plugin
version: 0.1.0
description: Single plugin shipped through its own marketplace

marketplace:
  owner:
    name: acme-org
    url: https://github.com/acme-org
  outputs:
    claude: {}
  packages:
    - name: my-plugin
      source: ./
      version: 0.1.0
```

`apm pack` writes the plugin bundle to `./build/my-plugin/` and the
marketplace artifact to `.claude-plugin/marketplace.json`. Commit
both. Consumers run `apm marketplace add acme-org/<repo>`.

## Aggregator

One repo whose only job is to curate plugins that live in other
repos. No plugin source lives here.

Scaffold:

```bash
apm marketplace init --owner acme-org --name acme-curated
apm marketplace package add acme-org/skill-pkg-a --version "^1.0.0"
apm marketplace package add acme-org/skill-pkg-b --ref v0.4.2
```

Resulting `apm.yml`:

```yaml
name: acme-curated
version: 1.0.0
description: Curated APM marketplace for acme-org

marketplace:
  owner:
    name: acme-org
    url: https://github.com/acme-org
  outputs:
    claude: {}
  packages:
    - name: skill-pkg-a
      source: acme-org/skill-pkg-a
      version: "^1.0.0"
    - name: skill-pkg-b
      source: acme-org/skill-pkg-b
      ref: v0.4.2
```

`apm pack` resolves every remote entry against `git ls-remote` and
writes `marketplace.json` only. No bundle is produced because there
is no `dependencies:` block.

## Monorepo-hybrid

> **Advanced.** Most first-time authors should start with single-plugin or aggregator. Reach for hybrid when you're shipping your own plugin *and* curating others. [`DevExpGbb/zava-agent-config`](https://github.com/DevExpGbb/zava-agent-config) is the live reference: 7 plugins under `plugins/`, one root `apm.yml`, releases via [microsoft/apm-action@v1](https://github.com/microsoft/apm-action) `mode: release`.

One repo, many plugins under `packages/`, one marketplace at the root
that lists them as local-path entries. Each plugin gets its own
`apm.yml` so it can be compiled and tested in isolation.

Layout:

```text
my-monorepo/
  apm.yml                          # marketplace + local-path packages
  packages/
    plugin-a/
      apm.yml                      # plugin-a's manifest
      .apm/
        agents/
          expert.agent.md
        instructions/
          style.instructions.md
        skills/
          my-skill/
            SKILL.md
    plugin-b/
      apm.yml
      .apm/
        prompts/
          review.prompt.md
        hooks/
          pre-tool.json
```

> **Important -- use `.apm/<type>/` for every primitive in each plugin.**
> When `.apm/` exists, it is the authoritative local pack source. Without
> `.apm/`, supported plugin-native root directories remain pack sources,
> including after `apm init` writes `includes: auto`. Mixed layouts pack from
> `.apm/` and warn about each skipped root source. `apm install` only discovers
> instructions, commands, and prompts under `.apm/<type>/`, so authoring
> `packages/plugin-a/instructions/style.instructions.md` instead of
> `packages/plugin-a/.apm/instructions/style.instructions.md` can install
> incomplete. See [Pack a bundle -- source layout and install-time
> discovery](../pack-a-bundle/#source-layout-and-install-time-discovery)
> for the full per-primitive scan-path reference.

Scaffold:

```bash
apm plugin init plugin-a --yes              # cd packages/plugin-a first
apm plugin init plugin-b --yes              # cd packages/plugin-b first
cd ../..
apm marketplace init --owner acme-org --name acme-monorepo
apm marketplace package add ./packages/plugin-a --name plugin-a
apm marketplace package add ./packages/plugin-b --name plugin-b
```

Resulting root `apm.yml`:

```yaml
name: acme-monorepo
version: 1.0.0
description: Acme plugins shipped together

marketplace:
  owner:
    name: acme-org
    url: https://github.com/acme-org
  outputs:
    claude: {}
  versioning:
    strategy: lockstep              # see versioning-strategies
  packages:
    - name: plugin-a
      source: ./packages/plugin-a
      version: 1.0.0
    - name: plugin-b
      source: ./packages/plugin-b
      version: 1.0.0
```

Local-path entries skip remote resolution. Each plugin's own
`apm.yml` controls its build; the root `apm.yml` controls the
marketplace index. Pick a versioning strategy that matches how you
tag releases -- see [Versioning strategies](../versioning-strategies/).

## Shipping `bin/` executables (Claude Code only)

A plugin may ship a top-level `bin/` directory of executable scripts.
When a consumer runs a **global** install (`apm install -g`), APM
deploys the plugin as a Claude Code skills-directory plugin (a folder
containing `.claude-plugin/plugin.json`) under the Claude skills
directory, which puts `bin/` on Claude Code's Bash tool `PATH`. The
agent can then invoke your scripts as bare commands.

```
my-plugin/
  apm.yml
  .apm/
  bin/
    my-tool                          # executable script (chmod handled by APM)
```

This is a **Claude-Code-specific** contract -- no other harness has an
equivalent -- so `bin/` deploys only when the consumer has an active
Claude Code skills target. Authoring rules:

- Deploy is **user-scope only**. A project-scope install (without `-g`)
  skips `bin/` and prints a hint to re-run with `-g`.
- APM tightens deployed executable files to **`0o700` on POSIX systems**.
  Do not rely on source-file permissions or a specific umask.
- Deployed executables sit on Claude Code's `PATH` and are invoked
  **without per-call confirmation**. Treat them as trusted code: keep
  them minimal, audited, and free of network side effects you would not
  want an agent to trigger unprompted.
- Enterprises can deny deployment per-package or globally via the org
  `executables.deny` policy (the legacy `bin_deploy` rule remains a
  deprecated alias) -- see the
  [policy schema](../../reference/policy-schema/#executables).

## What to read next

- [Versioning strategies](../versioning-strategies/) -- lockstep vs
  per-package and how `apm pack --check-versions` enforces them.
- [Releasing from any CI](../releasing-from-any-ci/) -- the canonical
  release pipeline that ships any of the shapes above.
- [Publish to a marketplace](../publish-to-a-marketplace/) -- the
  `apm marketplace init` walkthrough and the registry schema.
