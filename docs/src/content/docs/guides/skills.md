---
title: "Skills"
sidebar:
  order: 2
---

Skills (`SKILL.md`) are package meta-guides that help AI agents quickly understand what an APM package does and how to leverage its content. They provide a concise summary optimized for AI consumption.

## What are Skills?

Skills describe an APM package in a format AI agents can quickly parse:
- **What** the package provides (name, description)
- **How** to use it (body content with guidelines)
- **Resources** available (bundled scripts, references, examples)

### Skills Can Be Used Two Ways

1. **Package meta-guides for your own package**: Add a `SKILL.md` to your APM package to help AI agents understand what your package does
2. **Installed from Claude skill repositories**: Install skills from monorepos like `ComposioHQ/awesome-claude-skills` to gain new capabilities

When you install a package with a SKILL.md, AI agents can quickly understand how to use it.

## Installing Skills

### From Claude Skill Repositories

Many Claude Skills are hosted in monorepos. Install any skill directly:

```bash
# Install a skill from a monorepo subdirectory
apm install ComposioHQ/awesome-claude-skills/brand-guidelines

# Install skill with resources (scripts, references, etc.)
apm install ComposioHQ/awesome-claude-skills/skill-creator
```

## What Happens During Install

When you run `apm install`, APM handles skill integration automatically:

### Step 1: Download to apm_modules/
APM downloads packages to `apm_modules/owner/repo/` (or `apm_modules/owner/repo/skill-name/` for subdirectory packages).

### Step 2: Skill Integration
APM copies skills to every detected target directory:

| Package Type | Behavior |
|--------------|----------|
| **Has existing SKILL.md** | Entire skill folder copied to `{target}/skills/{skill-name}/` |
| **Has sub-skills in `.apm/skills/`** | Each `.apm/skills/*/SKILL.md` also promoted to `{target}/skills/{sub-skill-name}/` |
| **No SKILL.md and no primitives** | No skill folder created |

**Target Detection:**
- Recognized directories: `.github/`, `.claude/`, `.cursor/`, `.opencode/`, `.codex/`
- Codex skills deploy to `.agents/skills/` (agent skills standard directory), not `.codex/skills/`
- If none exist, `.github/` is created as the fallback
- Override with `--target`

### Skill Folder Naming

Skill names are validated per the [agentskills.io](https://agentskills.io/) spec:
- 1-64 characters
- Lowercase alphanumeric + hyphens only
- No consecutive hyphens (`--`)
- Cannot start/end with hyphen

```
.github/skills/
├── mcp-builder/           # From ComposioHQ/awesome-claude-skills/mcp-builder
└── apm-sample-package/    # From microsoft/apm-sample-package
```

### Step 3: Primitive Integration
APM also integrates prompts and commands from the package (using their original filenames).

### Installation Path Structure

Skills maintain their natural path hierarchy:

```
apm_modules/
└── ComposioHQ/
    └── awesome-claude-skills/
        └── brand-guidelines/      # Skill subdirectory
            ├── SKILL.md           # Original skill file
            ├── apm.yml            # Auto-generated
            └── LICENSE.txt        # Any bundled files
```

## SKILL.md Format

### Basic Structure

```markdown
---
name: Skill Name
description: One-line description of what this skill does
---

# Skill Body

Detailed instructions for the AI agent on how to use this skill.

## Guidelines
- Guideline 1
- Guideline 2

## Examples
...
```

### Required Frontmatter

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Display name for the skill |
| `description` | string | One-line description |

### Body Content

The body contains:
- **Instructions** for the AI agent
- **Guidelines** and best practices
- **Examples** of usage
- **References** to bundled resources

## Bundled Resources

Skills can include additional resources:

```
my-skill/
├── SKILL.md           # Main skill file
├── scripts/           # Executable code
│   └── validate.py
├── references/        # Documentation
│   └── style-guide.md
├── examples/          # Sample files
│   └── sample.json
└── assets/            # Templates, images
    └── logo.png
```

**Note:** All resources stay in `apm_modules/` where AI agents can reference them.

## Creating Your Own Skills

### Quick Start with apm init

`apm init` creates a minimal project:

```bash
apm init my-skill && cd my-skill
```

This creates:
```
my-skill/
├── apm.yml       # Package manifest
└── .apm/         # Primitives folder
```

Add a `SKILL.md` at root to make it a publishable skill (see below).

### Option 1: Standalone Skill

Create a repo with just `SKILL.md`:

```bash
mkdir my-skill && cd my-skill

cat > SKILL.md << 'EOF'
---
name: My Custom Skill
description: Does something useful
---

# My Custom Skill

## Overview
Describe what this skill does...

## Guidelines
- Follow these rules...

## Examples
...
EOF

git init && git add . && git commit -m "Initial skill"
git push origin main
```

Anyone can now install it:
```bash
apm install your-org/my-skill
```

### Option 2: Skill in APM Package

Add `SKILL.md` to any existing APM package:

```
my-package/
├── apm.yml
├── SKILL.md          # Add this for Claude compatibility
└── .apm/
    ├── instructions/
    └── prompts/
```

This creates a **hybrid package** that works with both APM primitives and Claude Skills.

### Option 3: Skills Collection (Monorepo)

Organize multiple skills in a monorepo:

```
awesome-skills/
├── skill-1/
│   ├── SKILL.md
│   └── references/
├── skill-2/
│   └── SKILL.md
└── skill-3/
    ├── SKILL.md
    └── scripts/
```

Users install individual skills:
```bash
apm install your-org/awesome-skills/skill-1
apm install your-org/awesome-skills/skill-2
```

### Option 4: Multi-skill Package

Bundle multiple skills inside a single APM package using `.apm/skills/`:

```
my-package/
├── apm.yml
├── SKILL.md              # Parent skill (package-level guide)
└── .apm/
    ├── instructions/
    ├── prompts/
    └── skills/
        ├── skill-a/
        │   └── SKILL.md  # Sub-skill A
        └── skill-b/
            └── SKILL.md  # Sub-skill B
```

On install, APM promotes each sub-skill to a top-level `.github/skills/` entry alongside the parent — see [Sub-skill Promotion](#sub-skill-promotion) below.

### Option 5: Maintainer-only Skill (Dev-only)

For skills you want during authoring but not shipped to consumers (release-checklist skills, internal debugging skills), author them OUTSIDE `.apm/` and reference them via a local-path devDependency:

```
your-package/
+-- apm.yml
+-- .apm/skills/...                          # public skills
+-- dev/skills/release-checklist/SKILL.md    # maintainer-only
```

```yaml
devDependencies:
  apm:
    - path: ./dev/skills/release-checklist
```

`apm install --dev` deploys the skill locally; `apm pack` excludes it from plugin output. See [Dev-only Primitives](../dev-only-primitives/) for the full pattern.

### Sub-skill Promotion

When a package contains sub-skills in `.apm/skills/*/` subdirectories, APM promotes each to a top-level entry under `.github/skills/`. This ensures Copilot can discover them independently, since it only scans direct children of `.github/skills/`.

```
# Installed package with sub-skills:
apm_modules/org/repo/my-package/
├── SKILL.md
└── .apm/
    └── skills/
        └── azure-naming/
            └── SKILL.md

# Result after install:
.github/skills/
├── my-package/              # Parent skill
│   └── SKILL.md
└── azure-naming/            # Promoted sub-skill
    └── SKILL.md
```

The same promotion applies to the project's own `.apm/skills/` directory. When you run `apm install`, skills in your local `.apm/skills/*/` are deployed to `.github/skills/` (and other detected targets) alongside dependency skills. Local skills take priority on collision. The root `SKILL.md` is not treated as a local skill -- it describes the project itself.

## Package Detection

APM automatically detects package types:

| Has | Type | Detection |
|-----|------|-----------|
| `apm.yml` only | APM Package | Standard APM primitives |
| `SKILL.md` only | Claude Skill | Treated as native skill |
| `hooks/*.json` only | Hook Package | Hook handlers only |
| Both files | Hybrid Package | Best of both worlds |

## Target Detection

APM deploys skills to every target directory that already exists: `.github/`, `.claude/`, `.cursor/`, `.opencode/`. For Codex (`.codex/`), skills deploy to `.agents/skills/` instead. If no target directories exist, `.github/` is created as the fallback.

Override with:
```bash
apm install skill-name --target claude
apm compile --target claude
```

Or set in `apm.yml`:
```yaml
name: my-project
target: vscode  # or claude, or all
```

## Best Practices

### 1. Clear Naming
Use descriptive, lowercase-hyphenated names:
- ✅ `brand-guidelines`
- ✅ `code-review-expert`
- ❌ `mySkill`
- ❌ `Skill_1`

### 2. Focused Description
Keep the description to one line:
- ✅ `Applies corporate brand colors and typography`
- ❌ `This skill helps you with branding and it can also do typography and it uses the company colors...`

### 3. Structured Body
Organize with clear sections:
```markdown
## Overview
What this skill does

## Guidelines
Rules to follow

## Examples
How to use it

## References
Links to resources
```

### 4. Resource Organization
Keep bundled files organized:
```
my-skill/
├── SKILL.md
├── scripts/      # Executable code only
├── references/   # Documentation
├── examples/     # Sample files
└── assets/       # Static resources
```

### 5. Version Control
Keep skills in version control. Use semantic versioning in the generated `apm.yml` for tracking.

## Integration with Other Primitives

Skills complement other APM primitives:

| Primitive | Purpose | Works With Skills |
|-----------|---------|-------------------|
| Instructions | Coding standards | Skills can reference instruction context |
| Prompts | Executable workflows | Skills describe how to use prompts |
| Agents | AI personalities | Skills explain what agents are available |
| Context | Project knowledge | Skills can link to context files |

## Troubleshooting

### Skill Not Installing

```
Error: Could not find SKILL.md or apm.yml
```

**Solution:** Verify the path is correct. For subdirectories, use full path:
```bash
apm install owner/repo/subdirectory
```

### Skill Name Validation Error

If you see a skill name validation warning:

1. **Check naming:** Names must be lowercase, 1-64 chars, hyphens only (no underscores)
2. **Auto-normalization:** APM automatically normalizes invalid names when possible

### Metadata Missing

If skill lacks APM metadata:

1. Check the skill was installed via APM (not manually copied)
2. Reinstall the package

## Related Documentation

- [Core Concepts](../../introduction/how-it-works/) - Understanding APM architecture
- [Primitives Guide](../../introduction/key-concepts/) - All primitive types
- [CLI Reference](../../reference/cli-commands/) - Full command documentation
- [Dependencies](../dependencies/) - Package management
