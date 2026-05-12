---
title: The Three Promises
description: The canonical spine APM is built around -- portable, secure, governed.
sidebar:
  order: 2
---

APM ships three promises. They are deliberately small and load-bearing.
Every command, every flag, every lockfile field exists to back one of
these three.

## Promise 1: Portable by manifest

One `apm.yml`. Seven harnesses. Reproducible AI agent setup.

Every developer who clones the repo runs `apm install` and gets the
same skills, prompts, instructions, hooks, and MCP servers wired into
Copilot, Claude, Cursor, OpenCode, Codex, Gemini, and Windsurf. The
lockfile pins exact versions and content hashes. New contributor
onboarding for AI context goes from "follow this 12-step README" to
one command.

The 10-second demo:

```bash
git clone <repo> && cd <repo> && apm install
```

### Proof in the source

- `src/apm_cli/models/apm_package.py` -- the `apm.yml` schema: one
  manifest, one set of dependencies, one set of `agentsmd` /
  `chatmodes` / `instructions` / `prompts` / `mcp` blocks consumed by
  every harness.
- `src/apm_cli/integration/targets.py` -- the registered harnesses an
  install fans out to (Copilot, Claude, Cursor, Codex, Gemini,
  OpenCode, Windsurf, plus VS Code and the `llm` CLI as delivery
  surfaces).
- `src/apm_cli/deps/lockfile.py` -- the `LockEntry.content_hash`
  field (SHA-256 of the package file tree) that makes "same install
  on every clone" mean byte-for-byte the same.

### Read more

- Lifecycle of an install: [Lifecycle](/apm/concepts/lifecycle/)
- Lockfile fields and semantics: [CLI commands](/apm/reference/cli/install/)

## Promise 2: Secure by default

Every `apm install` scans for hidden Unicode before agents read it.

Agent context is executable -- a prompt is a program for an LLM. APM
treats it that way. Each install scans for invisible Unicode that can
hijack agent behavior, pins content hashes in the lockfile, and gates
transitive MCP servers behind explicit trust prompts. `apm audit`
rebuilds context in scratch and diffs against your working tree to
catch hand-edits before they ship.

The 10-second demo:

```bash
apm audit
```

### Proof in the source

- `src/apm_cli/security/content_scanner.py` -- the `ContentScanner`
  class and the Unicode tag / bidi / zero-width / invisible-operator
  ranges it flags. `scan_text()` is what every install runs against
  every primitive file.
- `src/apm_cli/install/helpers/security_scan.py` -- the
  `_pre_deploy_security_scan` hook that runs before any file is
  written to the project tree, via `SecurityGate.scan_files()` with
  the install pipeline's `BLOCK_POLICY`.
- `src/apm_cli/deps/lockfile.py` -- `LockEntry.content_hash` pins the
  exact tree per dependency; `ci_checks._check_content_integrity`
  re-verifies it on every audit.
- `src/apm_cli/commands/audit.py` -- `apm audit` wires the scan,
  hash-drift detection, and the scratch rebuild diff into one
  command, with `--strip` to remediate.

### Read more

- Security model and threat coverage: [Security](/apm/enterprise/security/)

## Promise 3: Governed by policy

Org policy enforced at install time, before MCP touches disk.

`apm-policy.yml` lets a security team allow-list sources, scopes, and
primitives. Every `apm install` runs the policy *before* writing to
disk -- including transitive MCP servers shipped by deep
dependencies. Tighten-only inheritance flows enterprise -> org ->
repo. `apm audit --ci` wires the same checks into branch protection.
This is the supply-chain check npm and pip cannot do.

The 10-second demo:

```bash
apm install --dry-run <package>
```

### Proof in the source

- `src/apm_cli/policy/install_preflight.py` --
  `run_policy_preflight()` is the install-time gate; it evaluates the
  resolved dependency graph (including transitive MCP servers)
  against the merged policy before any download or write.
- `src/apm_cli/policy/inheritance.py` -- `merge_policies()` and
  `resolve_policy_chain()` implement the tighten-only enterprise
  -> org -> repo flow with `_escalate()` enforcement.
- `src/apm_cli/policy/ci_checks.py` -- `run_baseline_checks()` is
  the CI surface used by `apm audit --ci`. It runs 8 baseline
  checks: lockfile-exists, ref-consistency, deployed-files-present,
  no-orphans, skill-subset-consistency, config-consistency,
  content-integrity, and includes-consent.

### Read more

- Policy schema, inheritance, CI wiring: [Governance guide](/apm/enterprise/governance-guide/)

## FAQ

### Is this just `npm` for prompts?

The verbs rhyme on purpose -- `apm install`, `apm update`,
`apm list`, `apm prune`. The package model does not. APM resolves
primitives (skills, prompts, instructions, hooks, MCP servers) and
deploys them into seven different agent harnesses from one manifest.
npm has no equivalent of the harness fan-out, the install-time
policy gate, or the Unicode scan. Promise 1 is the npm-shaped half;
Promise 2 and Promise 3 are not.

### Why a lockfile?

Two reasons. First, reproducibility: pinned refs plus content
hashes mean every clone and every CI run gets the same files.
Second, integrity: `content_hash` lets `apm audit` detect any drift
between what the lockfile says you installed and what is on disk
right now -- including hand-edits to files inside `apm_modules/`.

### What does the policy engine actually block?

At install time: dependencies from disallowed sources or scopes,
primitives outside the allow-list, and transitive MCP servers that
fail any of the configured trust rules -- evaluated before any
download. In CI via `apm audit --ci`: the 8 baseline checks above,
which catch lockfile drift, missing deployed files, orphaned
packages, and content-hash mismatches before a PR can merge.
