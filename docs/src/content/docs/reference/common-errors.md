---
title: Common errors
description: Symptoms, causes, and fixes for frequently reported APM errors.
sidebar:
  order: 11
---

This page lists error messages and silent failures that users report most
frequently, along with their cause and the shortest path to a fix.

---

### MCP install: "No MCP-capable target in the declared target set"

**Symptom:** `apm install` exits with an error like:

```
[x] No MCP-capable target in the declared target set (agent-skills).
    Add an MCP-capable target (e.g., codex, copilot, claude) to install MCP dependencies.
```

**Cause:** Your `apm.yml` declares `dependencies.mcp:` entries but every
target in `targets:` lacks an MCP client adapter (for example `agent-skills`,
`grok-build`, `grok-cloud`, `openclaw`, `copilot-cowork`, or `copilot-app`
-- these are skills-only or workflow targets marked `[ ]` in the
[targets matrix](../targets-matrix/) **mcp** column).

**Fix:** Add at least one MCP-capable target (`codex`, `copilot`, `claude`,
`cursor`, `gemini`, `windsurf`, `kiro`, `vscode`, or `intellij`) alongside
the non-MCP target:

```yaml
targets:
  - codex        # MCP-capable
  - agent-skills # skills-only; MCP is skipped for this target
dependencies:
  mcp:
    - name: my-server
      registry: true
```

Alternatively, remove `dependencies.mcp:` entries if you only need skills
deployment.

---

### Cursor: "Config version must be a number"

**Symptom:** Cursor reports `Config version must be a number` or
`Failed to parse project hooks configuration`, or silently loads no project
hooks even though `apm install` succeeded and `.cursor/hooks.json` exists.

**Cause:** APM versions v0.14.1--v0.20.0 omitted the required top-level
`"version": 1` field from `.cursor/hooks.json`. Cursor rejects the entire
file when that field is absent.

**Fix:** Re-run hook integration to regenerate a valid config:

```bash
apm install --target cursor
```

Or, if you install all targets at once:

```bash
apm install
```

APM v0.21.0+ always writes `"version": 1` to `.cursor/hooks.json` on a
fresh install. Existing files that already contain a `"version"` key are
left untouched.
