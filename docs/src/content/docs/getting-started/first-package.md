---
title: "Your First Package"
description: "Create, publish, and install your first APM package in minutes."
sidebar:
  order: 3
---

This tutorial walks you through creating an APM package from scratch, publishing it, and installing it in another project.

## Prerequisites

- APM installed ([Installation guide](/apm/getting-started/installation/))
- A GitHub account and a repository to publish to

## 1. Scaffold the Package

```bash
apm init my-coding-standards
cd my-coding-standards
```

This creates:

```
my-coding-standards/
└── apm.yml              # Package manifest
```

> **Note:** By default, `apm init` creates only `apm.yml`. The directory structure below is what you build manually in the following steps. See [Anatomy of an APM Package](../../introduction/anatomy-of-an-apm-package/) for what `.apm/` is and why files live there.

## 2. Add an Instruction

Create a coding standard that applies to all Python files:

```bash
cat > .apm/instructions/python.instructions.md << 'EOF'
---
applyTo: "**/*.py"
---
# Python Standards
- Use type hints for all function parameters and return values
- Follow PEP 8 style guidelines
- Write docstrings for all public functions
- Prefer `pathlib.Path` over `os.path`
EOF
```

## 3. Add a Prompt

Create a reusable slash command:

```bash
cat > .apm/prompts/security-audit.prompt.md << 'EOF'
---
description: Run a security audit on the current file
---
Review this code for common security issues:
1. Input validation and sanitization
2. Authentication and authorization checks
3. Sensitive data exposure
4. SQL injection and XSS vulnerabilities
Provide specific line numbers and suggested fixes.
EOF
```

## 4. Update the Manifest

Edit `apm.yml` to describe your package:

```yaml
name: my-coding-standards
version: 1.0.0
description: Team coding standards and security prompts
```

## 5. Publish

Push to a git repository:

```bash
git init
git add .
git commit -m "Initial APM package"
git remote add origin https://github.com/you/my-coding-standards.git
git push -u origin main
```

## 6. Install in Another Project

In any project:

```bash
apm install you/my-coding-standards
```

APM automatically:
- Downloads the package to `apm_modules/`
- Copies instructions to `.github/instructions/`
- Copies prompts to `.github/prompts/`
- Updates `apm.yml` with the dependency

## 7. Optional: Compile for Other Tools

If you use tools beyond GitHub Copilot, Claude, Cursor, and OpenCode (which read deployed primitives natively), generate compiled instruction files:

```bash
apm compile
```

This produces `AGENTS.md` (for Codex, Gemini) and `CLAUDE.md` for tools that need a single instructions file. Copilot, Claude, and Cursor users can skip this step — OpenCode users need `apm compile` only if their packages include instructions (OpenCode reads `AGENTS.md` for those).

## Next Steps

- Add [skills](/apm/guides/skills/) to your package
- Set up [dependencies](/apm/guides/dependencies/) on other packages
- Distribute as a standalone plugin — see [Plugin authoring](../../guides/plugins/#plugin-authoring) and [Pack & Distribute](../../guides/pack-distribute/)
- Explore the [CLI reference](/apm/reference/cli-commands/) for more commands
