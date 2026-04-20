# Package Authoring

## Package directory structure

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

A **marketplace** is a curated index of packages (plugins) that consumers
install via `apm install <name>@<marketplace>`. Maintainers author a
`marketplace.yml` source file and compile it to an Anthropic-compliant
`marketplace.json` with `apm marketplace build`. Both files are committed.

### When to run `apm marketplace init`

- The user is setting up a new marketplace repository.
- The user wants to convert an ad-hoc list of plugins into a proper index.

Do NOT run `init` inside an existing package directory; a marketplace
repository is a separate repo whose job is to list plugins, not to be one.

### marketplace.yml shape

```yaml
name: my-marketplace
description: Short summary
version: 0.1.0
owner:
  name: acme-org
  url: https://github.com/acme-org
build:                       # APM-only, stripped at compile time
  tagPattern: "v{version}"
metadata:                    # pass-through, copied verbatim to marketplace.json
  homepage: https://example.com
packages:
  - name: example-package
    description: What this package does
    source: acme-org/example-package   # <owner>/<repo>
    version: "^1.0.0"                  # semver range OR 'ref:' below
    # ref: 3f2a9b1c                    # explicit SHA/tag/branch; overrides version
    # subdir: tools/x                  # optional subdirectory
    # tag_pattern: "{name}-v{version}" # optional per-package override
    # include_prerelease: false        # optional
```

Schema rules:
- `name`, `description`, `version`, `owner.name` are required.
- Each package needs either `version` (a semver range) or `ref` (explicit).
- `ref` takes precedence over `version`.
- Unknown keys raise a schema error -- do not invent fields.

### Build semantics

`apm marketplace build` runs `git ls-remote` against each package source,
picks the highest tag satisfying the range (under the applicable
`tagPattern`), and writes `marketplace.json`. The compiler:

1. Emits `plugins:` verbatim (Anthropic's key name).
2. Copies `metadata:` byte-for-byte.
3. Strips `build:`, per-package `version`, `tag_pattern`, `include_prerelease`.
4. Does not emit `versions[]` -- each plugin carries a single resolved ref.

Exit codes: `0` success, `1` build error, `2` schema error.

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
