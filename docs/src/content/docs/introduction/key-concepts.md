---
title: "Key Concepts"
sidebar:
  order: 4
---

Context components are the configurable tools that deploy proven prompt engineering and context engineering techniques. APM implements these as the core building blocks for reliable, reusable AI development workflows.

## How Context Components Work

APM implements Context - the configurable tools that deploy prompt engineering and context engineering techniques to transform unreliable AI interactions into engineered systems.

### Initialize a project with AI-Native structure

```bash
apm init my-project  # Creates complete Context scaffolding + apm.yml
```

### Generated Project Structure

```yaml
my-project/
├── apm.yml              # Project configuration and script definitions
├── SKILL.md             # Package meta-guide for AI discovery
└── .apm/
    ├── agents/          # Role-based AI expertise with tool boundaries
    │   ├── backend-dev.agent.md        # API development specialist
    │   └── frontend-dev.agent.md       # UI development specialist
    ├── instructions/    # Targeted guidance by file type and domain  
    │   ├── security.instructions.md    # applyTo: "auth/**"
    │   └── testing.instructions.md     # applyTo: "**/*test*"
    └── prompts/         # Reusable agent workflows
        ├── code-review.prompt.md       # Systematic review process
        └── feature-spec.prompt.md      # Spec-first development
```

### Intelligent Compilation

APM automatically compiles your primitives into optimized AGENTS.md files using mathematical optimization:

```bash
apm compile  # Generate optimized AGENTS.md files
apm compile --verbose  # See optimization decisions
```

**[Learn more about the Context Optimization Engine →](../../guides/compilation/)**

## Packaging & Distribution

**Manage like npm packages:**

```yaml
# apm.yml - Project configuration
name: my-ai-native-app
version: 1.0.0
scripts:
  impl-copilot: "copilot -p 'implement-feature.prompt.md'"
  review-copilot: "copilot -p 'code-review.prompt.md'" 
  docs-codex: "codex generate-docs.prompt.md -m github/gpt-4o-mini"
dependencies:
  mcp:
    - io.github.github/github-mcp-server
```

**Share and reuse across projects:**
```bash
apm install                    # Install dependencies and deploy primitives
apm compile                    # Generate optimized AGENTS.md files
```

## Overview

The APM CLI supports the following types of primitives:

- **Agents** (`.agent.md`) - Define AI assistant personalities and behaviors (legacy: `.chatmode.md`)
- **Instructions** (`.instructions.md`) - Provide coding standards and guidelines for specific file types
- **Skills** (`SKILL.md`) - Package meta-guides that help AI agents understand what a package does
- **Hooks** (`.json` in `.apm/hooks/` or `hooks/`) - Define lifecycle event handlers with script references
- **Plugins** (`plugin.json`) - Pre-packaged agent bundles auto-normalized into APM packages. Projects may use `apm.yml` only, `plugin.json` only, or both. See [Plugin authoring](../../guides/plugins/#plugin-authoring)

> **Note**: Both `.agent.md` (new format) and `.chatmode.md` (legacy format) are fully supported. VSCode provides Quick Fix actions to help migrate from `.chatmode.md` to `.agent.md`.

## File Structure

### Supported Locations

APM discovers primitives in these locations:

```
# APM-native structure
.apm/
├── agents/             # AI assistant definitions (new format)
│   └── *.agent.md
├── chatmodes/          # AI assistant definitions (legacy format)
│   └── *.chatmode.md
├── instructions/        # Coding standards and guidelines  
│   └── *.instructions.md
└── hooks/              # Lifecycle event handlers
    ├── *.json          # Hook definitions (JSON)
    └── scripts/        # Referenced scripts
        └── *.sh, *.py

# VSCode-compatible structure  
.github/
├── agents/             # VSCode Copilot agents (new format)
│   └── *.agent.md
├── chatmodes/          # VSCode Copilot chatmodes (legacy format)
│   └── *.chatmode.md
└── instructions/       # VSCode Copilot instructions
    └── *.instructions.md

# Generic files (anywhere in project)
*.agent.md
*.chatmode.md
*.instructions.md
```

## Component Types Overview

Context implements the complete [AI-Native Development framework](https://danielmeppiel.github.io/awesome-ai-native/docs/concepts/) through the following core component types:

### Instructions (.instructions.md)
**Context Engineering Layer** - Targeted guidance by file type and domain

Instructions provide coding standards, conventions, and guidelines that apply automatically based on file patterns. They implement strategic context loading that gives AI exactly the right information at the right time.

```yaml
---
description: Python coding standards and documentation requirements
applyTo: "**/*.py"
---
# Python Coding Standards
- Follow PEP 8 for formatting
- Use type hints for all function parameters
- Include comprehensive docstrings with examples
```

### Agent Workflows (.prompt.md)  
**Prompt Engineering Layer** - Executable AI workflows with parameters

Agent Workflows transform ad-hoc requests into structured, repeatable workflows. They support parameter injection, context loading, and validation gates for reliable results.

```yaml
---
description: Implement secure authentication system
mode: backend-dev
input: [auth_method, session_duration]
---
# Secure Authentication Implementation
Use ${input:auth_method} with ${input:session_duration} sessions
Review `security standards` before implementation
```

### Agents (.agent.md, legacy: .chatmode.md)
**Agent Specialization Layer** - AI assistant personalities with tool boundaries

Agents create specialized AI assistants focused on specific domains. They define expertise areas, communication styles, and available tools.

```yaml
---
description: Senior backend developer focused on API design
tools: ["terminal", "file-manager"]
expertise: ["security", "performance", "scalability"]
---
You are a senior backend engineer with 10+ years experience in API development.
Focus on security, performance, and maintainable architecture patterns.
```

> **File Format**: Use `.agent.md` for new files. Legacy `.chatmode.md` files continue to work and can be migrated using VSCode Quick Fix actions.

### Skills (SKILL.md)
**Package Meta-Guide Layer** - Quick reference for AI agents

Skills are concise summaries that help AI agents understand what an APM package does and how to leverage its content. They provide an AI-optimized overview of the package's capabilities.

```markdown
---
name: Brand Guidelines
description: Apply corporate brand colors and typography
---
# How to Use
When asked about branding, apply these standards...
```

**Key Features:**
- Install from Claude Skill repositories: `apm install ComposioHQ/awesome-claude-skills/brand-guidelines`
- Provides AI agents with quick understanding of package purpose
- Resources (scripts, references) stay in `apm_modules/`

→ [Complete Skills Guide](../../guides/skills/)

## Primitive Types

### Agents

Agents define AI assistant personalities and specialized behaviors for different development tasks.

**Format:** `.agent.md` (new) or `.chatmode.md` (legacy)

**Frontmatter:**
- `description` (required) - Clear explanation of the agent purpose
- `author` (optional) - Creator information
- `version` (optional) - Version string

**Example:**
```markdown
---
description: AI pair programming assistant for code review
author: Development Team
version: "1.0.0"
---

# Code Review Assistant

You are an expert software engineer specializing in code review.

## Your Role
- Analyze code for bugs, security issues, and performance problems
- Suggest improvements following best practices
- Ensure code follows team conventions

## Communication Style
- Be constructive and specific in feedback
- Explain reasoning behind suggestions
- Prioritize critical issues over style preferences
```

### Instructions

Instructions provide coding standards, conventions, and guidelines that apply to specific file types or patterns.

**Format:** `.instructions.md`

**Frontmatter:**
- `description` (required) - Clear explanation of the standards
- `applyTo` (required) - Glob pattern for file targeting (e.g., `"**/*.py"`)
- `author` (optional) - Creator information
- `version` (optional) - Version string

**Example:**
```markdown
---
description: Python coding standards and documentation requirements
applyTo: "**/*.py"
author: Development Team
version: "2.0.0"
---

# Python Coding Standards

## Style Guide
- Follow PEP 8 for formatting
- Maximum line length of 88 characters (Black formatting)
- Use type hints for all function parameters and returns

## Documentation Requirements
- All public functions must have docstrings
- Include Args, Returns, and Raises sections
- Provide usage examples for complex functions

## Example Format
```python
def calculate_metrics(data: List[Dict], threshold: float = 0.5) -> Dict[str, float]:
    """Calculate performance metrics from data.
    
    Args:
        data: List of data dictionaries containing metrics
        threshold: Minimum threshold for filtering
    
    Returns:
        Dictionary containing calculated metrics
    
    Raises:
        ValueError: If data is empty or invalid
    """
```

### Hooks

Hooks define lifecycle event handlers that run scripts at specific points during AI agent operations (e.g., before/after tool use).

**Format:** `.json` files in `hooks/` or `.apm/hooks/`

**Structure:**
```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": { "tool_name": "write_file" },
        "hooks": [
          {
            "type": "command",
            "command": "./scripts/lint-changed.sh $TOOL_INPUT_path"
          }
        ]
      }
    ]
  }
}
```

**Supported Events:** `PreToolUse`, `PostToolUse`, `Stop`, `Notification`, `SubagentStop`

**Integration:**
- VSCode: Hook JSON files are copied to `.github/hooks/*-apm.json` with script paths rewritten
- Claude: Hooks are merged into `.claude/settings.json` under the `hooks` key
- Scripts referenced by hooks are bundled alongside the hook definitions

## Discovery and Parsing

The APM CLI automatically discovers and parses all primitive files in your project.

## Validation

All primitives are automatically validated during discovery:

- **Agents**: Must have description and content (supports both `.agent.md` and `.chatmode.md`)
- **Instructions**: Must have description, applyTo pattern, and content

Invalid files are skipped with warning messages, allowing valid primitives to continue loading.

## Context Linking

Context files are **linkable knowledge modules** that other primitives can reference via markdown links, enabling composable knowledge graphs.

### Linking from Instructions

```markdown
<!-- .apm/instructions/api.instructions.md -->
---
applyTo: "backend/**/*.py"
description: API development guidelines
---

Follow `our API standards` and ensure
`GDPR compliance` for all endpoints.
```

### Linking from Agents

```markdown
<!-- .apm/agents/backend-expert.agent.md -->
---
description: Backend development expert
---

You are a backend expert. Always reference `our architecture patterns`
when designing systems.
```

### Automatic Link Resolution

APM automatically resolves context file links during installation and compilation:

1. **Discovery**: Scans all primitives for context file references
2. **Resolution**: Rewrites links to point to actual source locations
3. **Direct Linking**: Links point to files in `apm_modules/` and `.apm/` directories
4. **Persistence**: Commit `apm_modules/` for link availability, or run `apm install` in CI/CD

**Result**: Links work in IDE and GitHub, pointing directly to source files. Copilot and Claude resolve links natively via `apm install`; other tools pick them up through `apm compile`.

### Link Resolution Examples

Links are rewritten to point to actual source locations:

**From installed prompts/agents** (`.github/` directory):
```markdown
`API Standards`
→ `API Standards`
```

**From compiled AGENTS.md**:
```markdown
`Architecture`
→ `Architecture`
```

## Best Practices

### 1. Clear Naming
Use descriptive names that indicate purpose:
- `code-review-assistant.agent.md`
- `python-documentation.instructions.md`
- `team-contacts.md`

### 2. Targeted Application
Use specific `applyTo` patterns for instructions:
- `"**/*.py"` for Python files
- `"**/*.{ts,tsx}"` for TypeScript React files
- `"**/test_*.py"` for Python test files

### 3. Version Control
Keep primitives in version control alongside your code. Use semantic versioning for breaking changes.

### 4. Organized Structure
Use the structured `.apm/` directories for better organization:
```
.apm/
├── agents/
│   ├── code-reviewer.agent.md
│   └── documentation-writer.agent.md
└── instructions/
    ├── python-style.instructions.md
    └── typescript-conventions.instructions.md
```

### 5. Team Collaboration
- Include author information in frontmatter
- Document the purpose and scope of each primitive
- Regular review and updates as standards evolve

## Integration with VSCode

For VSCode Copilot compatibility, place files in `.github/` directories:
```
.github/
├── agents/
│   └── assistant.agent.md
└── instructions/
    └── coding-standards.instructions.md
```

These files follow the same format and will be discovered alongside APM-specific primitives. 

## Error Handling

The primitive system handles errors gracefully:

- **Malformed YAML**: Files with invalid frontmatter are skipped with warnings
- **Missing required fields**: Validation errors are reported clearly
- **File access issues**: Permission and encoding problems are handled safely
- **Invalid patterns**: Glob pattern errors are caught and reported

This ensures that a single problematic file doesn't prevent other primitives from loading.

## Spec Kit Constitution Injection (Phase 0)

When present, a project-level constitution file at `memory/constitution.md` is injected at the very top of `AGENTS.md` during `apm compile`.

### Block Format
```
<!-- SPEC-KIT CONSTITUTION: BEGIN -->
hash: <sha256_12> path: memory/constitution.md
<entire original file content>
<!-- SPEC-KIT CONSTITUTION: END -->
```

### Behavior
- Enabled by default; disable via `--no-constitution` (existing block preserved)
- Idempotent: re-running compile without changes leaves file unchanged
- Drift aware: modifying `memory/constitution.md` regenerates block with new hash
- Safe: absence of constitution does not fail compilation (status MISSING in Rich table)

### Why This Matters
Ensures downstream AI tooling always has the authoritative governance / principles context without manual copy-paste. The hash enables simple drift detection or caching strategies later.