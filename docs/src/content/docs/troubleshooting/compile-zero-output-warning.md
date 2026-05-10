---
title: "Compile produced no output"
description: "Diagnose why apm compile finished without writing any files."
sidebar:
  order: 3
---

`apm compile` exited cleanly but printed:

```text
[!] Compilation completed but produced no output files. Check that target
    directories exist (e.g. .github/, .claude/) or set 'target:' in apm.yml
    / pass --target explicitly.
```

This is APM refusing to claim success when zero files were written. The compile ran, the target list resolved, but every primitive was either missing, filtered, or unsupported by the resolved targets. Walk the ladder below in order.

## Diagnostic ladder

### 1. Confirm the resolved target list

```bash
apm targets
```

If the active list is empty, no harness was detected and no `target:` was pinned. APM has nothing to write against. Either:

- Create one of the canonical signals (`.claude/`, `.github/copilot-instructions.md`, `.cursor/`, `.codex/`, `.gemini/` or `GEMINI.md`, `.opencode/`, `.windsurf/`).
- Pin targets in `apm.yml` (`target: [claude, copilot]`) or pass `--target` to `apm compile`.

See [`apm targets`](../reference/cli/targets/) and the [manifest schema](../reference/manifest-schema/).

### 2. Confirm primitives are discovered

```bash
apm deps
```

Each installed package row shows per-type counts (`Prompts`, `Instructions`, `Agents`, `Skills`, `Hooks`). If every column is `-`, the project ships nothing to compile. Either you haven't installed a package yet, or your local `.apm/` tree is empty.

To check the local tree directly:

```bash
find .apm -name '*.instructions.md' -o -name '*.prompt.md' -o \
         -name '*.agent.md' -o -name '*.chatmode.md' -o -name 'SKILL.md'
```

### 3. Confirm the includes filter is not excluding everything

If `apm.yml` declares an `includes:` filter on a dependency, only matching primitives are integrated. A typo or overly narrow glob can silently drop the entire package contribution.

```yaml
dependencies:
  apm:
    - name: org/security-pack
      includes: [skills]   # prompts and instructions are excluded
```

See [manifest schema](../reference/manifest-schema/) for the full include/exclude semantics.

### 4. Confirm the target supports the primitive types you ship

This is the most common cause. Each target supports a subset of primitive types. Shipping only types a target rejects produces zero output for that target.

Authoritative matrix: [targets matrix](../reference/targets-matrix/).

[i] Ship only `prompts` to `claude`? Zero output -- Claude has no `prompts` slot.
[i] Ship only `instructions` to `codex`? Zero output -- Codex has no `instructions` slot.

### 5. Confirm policy is not dropping content

Org policy rules can deny specific primitives, sources, or transformers. If a policy denies what would otherwise be written, the file count drops to zero.

```bash
apm policy status
```

See [policy schema](../reference/policy-schema/).

## Worked examples

### "I added a skill but compile produced nothing"

```bash
apm targets        # what will be written to?
apm deps           # is the skill counted?
```

If `apm targets` is empty, the project has no detected harness -- create `.claude/` (or any canonical dir) or pin `target:` in `apm.yml`. If `apm deps` shows the skill but compile still emits nothing, every active target lacks `skills` support (rare -- most targets do support skills via `.agents/SKILL.md`).

### "I targeted `cursor` but my command files don't appear"

Cursor commands route through the shared `claude_command` transformer, which only preserves the common frontmatter subset (`description`, `allowed-tools`, `model`, `argument-hint`, `input`). If your command file has *only* Cursor-specific keys (`author`, `mcp`, `parameters`, ...), nothing the transformer recognises is left to write. Add at least a `description:` field, then re-run:

```bash
apm compile --target cursor
```

Dropped keys are surfaced via diagnostics at install time -- check `apm install` output for warnings.

### "All targets resolve, but only some integrations land"

Expected when targets and primitive types don't intersect uniformly. Example: a project with `target: [claude, gemini]` shipping `instructions` + `commands` writes:

- `claude`: instructions (as rules) + commands -- both land.
- `gemini`: commands only -- instructions are silently skipped because Gemini has no `rules/` slot.

Cross-check against the [targets matrix](../reference/targets-matrix/) before filing a bug.

## Still nothing?

Re-run with `--verbose` for source attribution and per-target stats:

```bash
apm compile --verbose
```

If `*_files_written` and `*_files_generated` are all zero across the stats block, every target was a no-op. Use the ladder above to find which step silently filtered everything out.

Reference: [`apm compile`](../reference/cli/compile/), [`apm targets`](../reference/cli/targets/), [`apm view`](../reference/cli/view/), [targets matrix](../reference/targets-matrix/), [manifest schema](../reference/manifest-schema/), [policy schema](../reference/policy-schema/).
