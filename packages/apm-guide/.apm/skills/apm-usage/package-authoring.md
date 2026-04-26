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
