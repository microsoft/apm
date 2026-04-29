# Package Authoring

## Supported package layouts

APM recognizes three layouts. The shape of the package root tells APM
how to install it:

| Root signal | Author intent | Install semantic |
|---|---|---|
| `.apm/` (with or without apm.yml) | Multiple independent primitives | Hoist each primitive into the consumer runtime dirs |
| `SKILL.md` (alone, or with apm.yml = HYBRID) | One skill bundle | Copy whole tree to `<target>/skills/<name>/` |
| `plugin.json` / `.claude-plugin/` | Claude plugin collection | Dissect via plugin artifact mapping |

The HYBRID layout (apm.yml + SKILL.md) is a single skill bundle that
also uses APM dependency resolution. APM installs it as a skill -- it
does NOT dissect the bundle into top-level primitives. Co-located
subdirectories like `agents/`, `assets/`, `scripts/` are bundle
resources, not standalone primitives.

In a HYBRID package, `apm.yml` and `SKILL.md` each own their
`description` field **independently** -- APM never merges or
backfills one from the other:
- `apm.yml.description` is a short human-facing tagline rendered by
  `apm view`, `apm search`, `apm deps list`, and registry listings.
- `SKILL.md` `description` (frontmatter) is the agent-runtime
  invocation matcher (per agentskills.io). APM copies `SKILL.md`
  byte-for-byte and never reads or mutates this field.
- `allowed-tools` lives exclusively in `SKILL.md` frontmatter; there
  is no apm.yml-side equivalent.
- `name`, `version`, `license`, `dependencies`, `scripts` live
  exclusively in `apm.yml`.

Populate both descriptions when you ship a HYBRID package. `apm pack`
warns when `apm.yml.description` is missing so listings do not
degrade silently while the agent runtime keeps working.

## Package directory structure (APM layout)

```
my-package/
  apm.yml                              # package manifest (required)
  .apm/                                # local primitives directory
    instructions/
      security.instructions.md
      python.instructions.md
    chatmodes/
      architect.chatmode.md
    contexts/
      codebase.context.md
    prompts/
      code-review.prompt.md
    agents/
      reviewer.agent.md
    skills/
      my-skill/
        SKILL.md
        resource1.md
        resource2.md
```

## Manifest fields: `target:` validation contract

The `target:` field in `apm.yml` controls which output runtimes the package
compiles and installs to. Both `apm.yml`'s `target:` and the `--target` CLI
flag share the same validator, so identical input is rejected or accepted
the same way at every entry point. Invalid values fail at parse time with a
message naming the apm.yml path and the offending token -- they do **not**
silently fall through to auto-detect.

| Form | Behaviour |
|------|-----------|
| `target: copilot` | Single token; allowed values: `vscode`, `agents`, `copilot`, `claude`, `cursor`, `opencode`, `codex`, `all` |
| `target: [claude, copilot]` | List form; only listed targets are compiled/installed |
| `target: claude,copilot` | CSV-string form; parses identically to the list form (the shared validator splits on `,`). Before #820 was fixed, this silently produced zero deployment |
| `target:` omitted entirely | Auto-detect from project folders (`.github/`, `.claude/`, `.codex/`) |
| `target: bogus` (unknown token) | **Parse error** -- fix the typo |
| `target: ""` or `target: []` (empty) | **Parse error** -- remove the line if you meant auto-detect |
| `target: [all, claude]` (`all` mixed with other targets) | **Parse error** -- use `all` alone |

Error messages always name the `apm.yml` path and the offending token, so the
fix point is unambiguous. The list form (`target: [a, b]`) is the recommended
shape; the CSV-string form is supported for parity with `--target a,b` on the
CLI but reads less cleanly in YAML.

## The 7 primitive types

### 1. Instruction (`*.instructions.md`)

Contextual guidance scoped to file patterns.

```yaml
---
description: "Security best practices for Python"
applyTo: "**/*.py"
tags: [security, validation]
---
```

### 2. Chatmode (`*.chatmode.md`)

Chat persona configuration.

```yaml
---
name: "architect"
description: "System architecture expert"
system_prompt: "You are an expert..."
temperature: 0.7
---
```

### 3. Context (`*.context.md`)

Domain knowledge and background information.

```yaml
---
description: "Company coding standards"
applyTo: "**/*"
---
```

### 4. Prompt / Agent Workflow (`*.prompt.md`)

Executable workflows with parameters.

```yaml
---
description: "Code review workflow"
model: "gpt-4"
parameters:
  - name: pr_url
    description: "GitHub PR URL"
    required: true
---
```

### 5. Agent (`*.agent.md`)

Agent persona and behavior definition.

```yaml
---
name: "code-reviewer"
description: "Reviews code for quality"
instructions: |
  Focus on:
  - Security
  - Performance
---
```

### 6. Skill (folder-based, `SKILL.md`)

Reusable capability with supporting resources.

```
my-skill/
  SKILL.md                             # skill metadata and entry point
  resource1.md                         # supporting documentation
  resource2.md
```

### 7. Marketplace Plugin (`plugin.json`)

Packaged distribution format created with `apm pack --format plugin`.

## Step-by-step: create and publish

```bash
# 1. Initialize a package project
apm init my-package --plugin

# 2. Add primitives to .apm/ subdirectories
#    (instructions, agents, prompts, skills, etc.)

# 3. Test locally
apm install ./my-package               # install from local path
apm compile --verbose                  # verify compilation output

# 4. Validate
apm audit                              # check for security issues
apm audit --ci                         # run baseline CI checks

# 5. Publish
#    Push to a Git repository (GitHub, GitLab, ADO)
git init && git add . && git commit -m "Initial package"
git remote add origin git@github.com:org/my-package.git
git push -u origin main
git tag v1.0.0 && git push --tags

# 6. Consumers install via
apm install org/my-package#v1.0.0
```

## Marketplace authoring

A **marketplace** is a curated index of plugins that consumers install via
`apm install <name>@<marketplace>`. Maintainers declare the marketplace in a
`marketplace:` block inside `apm.yml`; running `apm pack` builds an
Anthropic-compliant `.claude-plugin/marketplace.json`. Both files are committed.

### When to run `apm marketplace init`

- The user is setting up a new marketplace repository.
- The user wants to convert an ad-hoc list of plugins into a proper index.

`apm marketplace init` appends a `marketplace:` block to the project's
`apm.yml` and creates `.claude-plugin/`. It does NOT scaffold a standalone
`marketplace.yml`. Use `apm init --marketplace` when starting a brand-new
project that will publish its own marketplace.

### apm.yml `marketplace:` block

```yaml
name: my-project
version: 0.1.0
description: Short summary

marketplace:
  # name / description / version inherit from apm.yml top level
  # (omit unless you need to override).
  owner:
    name: acme-org
    url: https://github.com/acme-org
  build:                       # APM-only, stripped at compile time
    tagPattern: "v{version}"
  metadata:                    # pass-through, copied verbatim
    homepage: https://example.com
  plugins:
    - name: example-plugin
      description: What this plugin does
      source: acme-org/example-plugin    # owner/repo (remote)
      version: "^1.0.0"                  # semver range OR 'ref:' below
      # ref: 3f2a9b1c                    # explicit SHA/tag/branch
      # subdir: tools/x                  # optional subdirectory
      # tag_pattern: "{name}-v{version}" # optional per-plugin override
      # include_prerelease: false        # optional

    - name: local-tool
      description: Plugin shipped alongside this repo
      source: ./plugins/local-tool       # local path (no remote fetch)
      version: 0.1.0
```

Schema rules:
- `owner.name` is required. `name`, `description`, `version` are
  optional inside the block (inherited from apm.yml top level).
- Each remote plugin needs either `version` or `ref`.
- `ref` takes precedence over `version`.
- `source: ./...` marks a local-path entry: skips git resolution,
  emits the path verbatim into `marketplace.json`.
- Unknown keys raise a schema error -- do not invent fields.

### Build semantics

`apm pack` runs `git ls-remote` against each remote plugin source, picks the
highest tag satisfying the range (under the applicable `tagPattern`), leaves
local-path entries untouched, and writes `.claude-plugin/marketplace.json`.
The compiler:

1. Emits `plugins:` verbatim (Anthropic's key name).
2. Copies `metadata:` byte-for-byte.
3. Strips `build:`, per-plugin `version`, `tag_pattern`, `include_prerelease`.
4. Omits empty `tags:` and inherited top-level `description`/`version`
   from the output (matches Anthropic's canonical hand-authored shape,
   e.g. microsoft/azure-skills).
5. Does not emit `versions[]` -- each plugin carries a single resolved ref.

`apm pack` also produces a bundle if `apm.yml` declares `dependencies:`. With
only a `marketplace:` block present, bundle flags (`--archive`, `-o`, `--format`,
`--target`, `--force`) are silent no-ops.

Marketplace-relevant flags on `apm pack`: `--dry-run`, `--offline`,
`--include-prerelease`, `--marketplace-output PATH`, `-v`.

Exit codes: `0` success, `1` build error, `2` schema error.

### Migrating from legacy `marketplace.yml`

Earlier APM versions stored this configuration in a standalone
`marketplace.yml`. That file is deprecated; `apm marketplace init` no longer
creates one. Run the one-shot migration:

```bash
apm marketplace migrate --dry-run    # preview the apm.yml change
apm marketplace migrate --yes        # apply: rewrite apm.yml, delete marketplace.yml
```

`--force`, `--yes`, and `-y` are equivalent. Both files present at once
is a hard error -- run `migrate` to consolidate.

### Full guide

See [docs/guides/marketplace-authoring](../../../../../docs/src/content/docs/guides/marketplace-authoring.md)
for the complete maintainer workflow (quickstart, version ranges, `check`,
`doctor`, `outdated`, and `publish`).

## Org-wide packages

For organization-wide standards, create a single repository with shared
primitives and have all team repos depend on it:

```yaml
# In each team repo's apm.yml
dependencies:
  apm:
    - contoso/engineering-standards#v2.0.0
```

This ensures consistent instructions, agents, and policies across the org.
Local `.apm/` primitives in each repo can extend or override the shared ones
(local always takes priority over dependencies).
