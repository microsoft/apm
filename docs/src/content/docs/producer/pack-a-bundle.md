---
title: Pack a bundle
description: Build a plugin-format bundle from APM-native or plugin-native source so others can deploy it with one apm install command.
---

A bundle is the artifact you hand to a consumer when you do not want to publish
to a registry. It is a directory (or archive -- `.zip` by default, `.tar.gz` via
`--archive-format tar.gz`) containing a
`plugin.json`, your primitive folders, and an embedded `apm.lock.yaml` that
pins every file by SHA-256. Build it with one command from a project that has
`apm.yml`:

```bash
apm pack
```

This is the producer side of [Deploy a local bundle](../consumer/deploy-a-bundle/).
Consumers who receive the artifact run `apm install ./your-bundle` and skip
the registry resolver entirely.

## What `apm pack` produces

By default `apm pack` writes a plugin-format directory under `./build/`:

```
build/<your-package>/
+-- plugin.json
+-- agents/
+-- skills/
+-- commands/
+-- hooks/
+-- apm.lock.yaml      # embedded: pins every file by SHA-256
```

The success line tells you exactly what to share:

```
$ apm pack
[+] Packed 7 file(s) -> build/my-pkg
[>] Plugin bundle ready -- contains plugin.json plus plugin-native
    directories (agents/, skills/, commands/, ...) and an embedded
    apm.lock.yaml for install-time integrity verification.
[i] Share with: apm install build/my-pkg
```

Add `--archive` to get a single archive (`.zip` by default; use `--archive-format tar.gz`
for legacy CI pipelines) instead of a directory; use `-o` to change the output location
(default `./build`).

:::tip[Windows-native handoff]
ZIP archives are natively extractable on Windows -- no WSL, tar, or additional
tooling required. That is why `.zip` is the default archive format.
:::

```bash
apm pack --archive -o ./dist
# -> ./dist/my-pkg-<version>.zip
```

## The plugin.json contract

`plugin.json` is the bundle's identity card. Only `name` is required. APM
synthesises one from `apm.yml` if you do not author it yourself, mapping these
fields:

| `apm.yml` field | `plugin.json` field |
|---|---|
| `name`         | `name` (required) |
| `version`      | `version` |
| `description`  | `description` |
| `author`       | author |
| `license`      | `license` |
| `homepage`     | `homepage` |
| `repository`   | `repository` |
| `keywords`     | `keywords` |

The `author` field accepts a plain string (`"Jane Doe"` maps to `{name: "Jane Doe"}`) or a
structured object (`{name, email?, url?}` -- all keys optional except `name`):

```yaml
# String form (backward-compatible):
author: Jane Doe

# Structured form:
author:
  name: Jane Doe
  email: jane@example.com
  url: https://example.com/jane
```

Author your own `plugin.json` at the project root (or under `.github/plugin/`,
`.claude-plugin/`, or `.cursor-plugin/`) when you need fields APM does not
synthesise -- otherwise leave it to `apm pack` and keep `apm.yml` as the
source of truth. See [Package anatomy](../concepts/package-anatomy/) for
the full schema.

## Integrity: how install verifies the bundle

`apm pack` writes `pack.bundle_files` into the embedded `apm.lock.yaml` -- a
mapping of every file's relative path to its SHA-256 digest. On the consumer
side, `apm install <bundle>` rehashes every file and rejects the bundle if:

- any hash does not match
- any file listed in `pack.bundle_files` is missing
- any file is present in the bundle but not listed in the manifest
- any path is a symlink

The manifest is the source of truth. Tampering after pack time is detected
before any file lands in the project. You do not configure this -- it runs on
every `apm install <bundle>`.

## Distribution paths

Three common ways to hand off a bundle:

- **Directory + git.** Commit `build/<pkg>/` to a release branch or a separate
  artifacts repo. Consumers `git clone` and run `apm install ./build/<pkg>`.
- **Archive + GitHub release.** `apm pack --archive` then upload the
  `.zip` as a release asset. Consumers download and run
  `apm install ./<pkg>-<version>.zip`.
- **Marketplace entry.** If your project also has a `marketplace:` block in
  `apm.yml`, `apm pack` builds `marketplace.json` alongside the bundle. See
  [Publish to a marketplace](./publish-to-a-marketplace/).

For the consumer flags that apply (`--target`, `--global`, `--force`,
`--dry-run`), see [Deploy a local bundle](../consumer/deploy-a-bundle/).

## Source layout and install-time discovery

When `.apm/` exists, local primitive content is sourced from `.apm/`.
Without `.apm/`, supported plugin-native root directories such as
`agents/`, `skills/`, `commands/`, and `hooks/` remain pack sources.
`includes: auto` grants publication consent but does not select either
layout. An explicit `includes` list is exhaustive and may deliberately
name an APM-native or root path.

If both layouts exist, `.apm/` wins and packing succeeds. A warning names
each skipped root source and tells you to move it under `.apm/` or remove
it:

```text
[!] Skipping root-level skills/ because .apm/ is present.
    Move publishable files to .apm/skills/ or remove skills/ to silence
    this warning.
```

When packing git dependencies, `apm pack` emits **only** what the
lockfile attests, in every format (`--format plugin` and the default
`--format apm`). Dependency content is sourced exclusively from the
lockfile `deployed_files` list -- the `apm_modules` cache is never
packed, because it carries no provenance or integrity guarantee (it can
be stale, partial, or tampered). Each attested file is verified against
its recorded `deployed_file_hashes` SHA-256 before it is included; a file
whose bytes no longer match its recorded hash fails the pack. (Files from
an older lockfile that predates `deployed_file_hashes` have no recorded
hash and pack without verification.) If a dependency declares
[`skills:`](../reference/package-types/#skill-collection-skillsnameskillmd),
only the named deployed skills are included. If a dependency has cached
primitives but no `deployed_files` (a stale or partial install), `apm
pack` fails and tells you to run `apm install` to record provenance.

Dependency **hooks-config and MCP-config** (the `hooks.json` / `.mcp.json`
entries `apm install` merges into shared host settings) are *not*
attested in `deployed_files`, so they are not packed; `apm pack` warns
loudly (`[!]`) and names the dependency when this happens. First-party hooks
authored by the packaging project follow the selected local layout: root
`hooks/` and `hooks.json` are skipped when `.apm/` is present. The packaging
project's root `.mcp.json` is packed independently of source layout. Only
unattested dependency config is dropped. Hook *scripts* recorded in
`deployed_files` still pack normally.

`apm install` is per-primitive and stricter. Each integrator has its own
discovery rules. For some primitive types the root convention directory
is not scanned at install time, so a file that appears in the pack
bundle may be silently skipped by a downstream `apm install` call.

The table below shows what `apm install` actually scans for each
primitive type:

| Primitive | `apm install` scans | Root alternative accepted? |
|-----------|---------------------|---------------------------|
| instruction | `.apm/instructions/*.instructions.md` | No |
| command (prompt) | `.apm/prompts/*.prompt.md` | No |
| hook | `.apm/hooks/*.json` | Yes: `hooks/*.json` |
| agent | `.apm/agents/**/*.agent.md` | Yes: `*.agent.md` at package root |
| skill | `.apm/skills/<name>/SKILL.md` | Yes: `skills/<name>/SKILL.md` (SKILL_BUNDLE or MARKETPLACE_PLUGIN) |

Source: `src/apm_cli/integration/instruction_integrator.py`,
`src/apm_cli/integration/command_integrator.py`,
`src/apm_cli/integration/hook_integrator.py`,
`src/apm_cli/integration/agent_integrator.py`,
`src/apm_cli/integration/skill_integrator.py`.

### Canonical layout for marketplace publishers

:::caution[Silent install drops can remove intended guardrails]
When `.apm/` exists, it is the authoritative local source. Without `.apm/`,
supported plugin-native root directories remain pack sources, including after
`apm init` writes `includes: auto`. An explicit [`includes`
list](../reference/manifest-schema/#39-includes) is exhaustive regardless
of layout.
`apm install` does NOT discover instructions, commands, or prompts placed
in root convention directories. Packages that rely on these primitives for
security guardrails or policy enforcement will install silently incomplete,
potentially removing those guardrails from consumer environments.
:::

If you publish a plugin that consumers install via `apm install`, use
`.apm/<type>/` for **every** primitive type. This layout is the only
one that works symmetrically through both `apm pack` (export) and
`apm install` (discovery).

```
plugins/my-plugin/
  apm.yml                          # minimal: name, version, description
  .apm/
    agents/
      security.agent.md
    skills/
      my-skill/
        SKILL.md
    instructions/
      style.instructions.md        # ONLY discovered from .apm/instructions/
    prompts/
      review.prompt.md             # ONLY discovered from .apm/prompts/
    hooks/
      pre-tool.json
```

To verify what your bundle actually contains before distributing it,
run:

```bash
apm pack --dry-run --verbose
```

The verbose output lists every file and any path remappings. Any
instruction or prompt you expect to be included should appear there
before you share the bundle.

### Multi-plugin marketplace publisher

When one repo ships multiple plugins and a marketplace index, give each
plugin its own `apm.yml` and `.apm/<type>/` source tree:

```
my-publisher-repo/
  apm.yml                          # root: marketplace: block only
  plugins/
    plugin-a/
      apm.yml                      # per-plugin manifest
      .apm/
        agents/
          expert.agent.md
        instructions/
          rules.instructions.md
    plugin-b/
      apm.yml
      .apm/
        skills/
          my-skill/
            SKILL.md
```

Per-plugin `apm pack` (run from each plugin directory) emits the plugin
bundle. The root `apm pack` builds the marketplace index. See
[Repo shapes](./repo-shapes/) for the full layout options.

## Pitfalls

**Do not use `--format apm` for bundles you expect consumers to install.**
The legacy APM bundle layout has no `plugin.json` and `apm install` rejects
it with a targeted error. The flag exists for tooling that still consumes
the older layout; new bundles should use the default `--format plugin`. If
you only have a legacy artifact, repack it:

```bash
apm pack --format plugin --archive
```

**Do not set `--target`.** The flag is deprecated. Bundles are
target-agnostic: the consumer's project decides which harness layouts
receive files at install time. APM records the value in `pack.target` as
informational metadata only and prints a deprecation warning.

**Missing dependency content.** For installed dependencies, `apm pack`
emits only lockfile-attested `deployed_files`; the `apm_modules` cache is
never packed. If a dependency has cached primitives but no
`deployed_files`, `apm pack` stops with an error and tells you to run
`apm install` so the content is attested.

**Attested-file mismatch.** If a dependency file was edited or corrupted
after `apm install`, its bytes no longer match the `deployed_file_hashes`
SHA-256 recorded in `apm.lock.yaml` and `apm pack` fails with
`... does not match the hash recorded in apm.lock.yaml`. A deleted file
raises a sibling error that also points at `apm install` (the exact
wording differs between `--format apm` and `--format plugin`). Run
`apm install` to restore the attested content, then pack again.

**Dry-run before sharing.** Use `apm pack --dry-run --verbose` to see the
full file list (and any path remappings) without writing anything.

## What to read next

- [Deploy a local bundle](../consumer/deploy-a-bundle/) -- the consumer
  side of this hand-off.
- [Publish to a marketplace](./publish-to-a-marketplace/) -- when a registry
  entry is a better fit than a bundle.
- [Package anatomy](../concepts/package-anatomy/) -- the file layout and
  schema reference.
