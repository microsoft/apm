---
title: apm audit
description: Scan installed primitives for hidden Unicode, drift, and lockfile/policy violations.
sidebar:
  order: 14
---

## Synopsis

```bash
apm audit [PACKAGE] [OPTIONS]
```

## Description

`apm audit` is the explicit security and integrity tool. It runs in two modes:

- **Content scan mode** (default). Scans deployed prompt, instruction, skill, and rules files for hidden Unicode that can embed invisible instructions in agent context, and replays the install pipeline into a scratch tree to detect drift (hand-edits to deployed files, missing integrations, orphaned files vs the lockfile). Can also remediate findings with `--strip` or scan an arbitrary file with `--file`.
- **CI gate mode** (`--ci`). Runs lockfile consistency checks plus drift in machine-readable form (text, JSON, or SARIF) suitable for branch-protection gates. Auto-discovers org policy from your project's git remote unless `--no-policy` is set.

This is the explicit power tool. Built-in protection against critical Unicode findings already runs automatically in `apm install`, `apm compile`, and `apm unpack`; you do not need to call `apm audit` to be safe by default. See [Drift and secure by default](../../../consumer/drift-and-secure-by-default/) for the consumer-side overview and [Enforce in CI](../../../enterprise/enforce-in-ci/) for the gating workflow. For marketplace plugin transitive-dependency pinning, see [`apm marketplace audit`](../marketplace/#apm-marketplace-audit-name).

`PACKAGE`, when supplied, is the lockfile package key (the repo URL) of a single installed dependency to scan. Omit it to scan every installed package plus local `.apm/` content.

## Options

### Content scan

| Flag | Default | Description |
|---|---|---|
| `--file PATH` | unset | Scan an arbitrary file instead of installed packages. Bypasses drift detection. |
| `--strip` | off | Remove critical and warning severity characters in place. Preserves emoji and ZWJ inside emoji sequences. |
| `--dry-run` | off | Preview what `--strip` would remove without modifying files. |
| `--no-drift` | off | Skip the install-replay drift check (reduces coverage). Mutually exclusive with `--strip` and `--file`. |
| `--verbose`, `-v` | off | Show info-level findings and per-file detail. No effect in `--ci` mode. |

### External scanners (experimental)

Gated by the `external-scanners` experimental flag (`apm experimental enable external-scanners`). Folds findings from any SARIF 2.1.0 scanner into the report. CLI-driven and install-method-neutral — no pip extra; works with the APM binary. See [External scanners](../../../integrations/external-scanners/).

| Flag | Default | Description |
|---|---|---|
| `--external NAME` | unset | Ingest findings from an external SARIF-native scanner (repeatable). Names: `skillspector` (invokes the CLI on `PATH`), `sarif` (ingests a file via `--external-sarif`). Cannot be combined with `--strip`, `--dry-run`, or `--ci`. |
| `--external-sarif PATH` | unset | SARIF file to ingest for `--external sarif`. |
| `--external-llm` / `--no-external-llm` | adapter default | Force a scanner's LLM-powered analysis on or off for this run (overrides config). SkillSpector default is offline `--no-llm`. LLM mode makes outbound API calls and needs `OPENAI_API_KEY` or `NVIDIA_INFERENCE_KEY`; missing the key fails closed. Requires `--external`. |
| `--external-args TEXT` | unset | Extra scanner CLI flags as a single shlex-split string (e.g. `"--model gpt-4o"`). Allowlist-validated per adapter; secret-looking or out-of-cwd tokens are rejected fail-closed. Overrides config args. Requires `--external`. |

When LLM mode is active APM prints a `[!]` egress banner before the scan noting that outbound API calls will be made. `--external-llm` / `--external-args` used without `--external <name>` raise a usage error (exit 2).

### Output

| Flag | Default | Description |
|---|---|---|
| `--format`, `-f text\|json\|sarif\|markdown` | `text` | Output format. `sarif` targets GitHub Code Scanning. `markdown` is for GitHub step summaries and is not allowed with `--ci`. |
| `--output`, `-o PATH` | stdout | Write the report to a file. Format is auto-detected from extension (`.sarif`, `.sarif.json`, `.json`, `.md`) when `--format` is omitted. |

### CI gate

| Flag | Default | Description |
|---|---|---|
| `--ci` | off | Run lockfile consistency checks and drift as a CI gate. Cannot be combined with `--strip`, `--dry-run`, `--file`, or `PACKAGE`. |
| `--policy SOURCE` | auto | Policy source for `--ci`. Accepts `org` (auto-discover from the project's git remote), `owner/repo`, an `https://` URL, or a local file path. Experimental. Without `--ci` it is ignored with a warning. |
| `--no-policy` | off | Skip policy discovery and enforcement. Equivalent to `APM_POLICY_DISABLE=1`. Overridden when `--policy` is passed explicitly. |
| `--no-cache` | off | Force a fresh policy fetch (skip the policy cache). Only relevant with policy discovery active. |
| `--no-fail-fast` | off | Run every check even after the first failure, for a full diagnostic report. |

## Examples

### Default audit (content scan plus drift)

```bash
apm audit
```

### Scan a specific installed package

```bash
apm audit https://github.com/owner/repo
```

### Scan an arbitrary file outside APM

```bash
apm audit --file .cursorrules
```

### Remediate findings

```bash
# Preview what --strip would remove
apm audit --strip --dry-run

# Strip critical and warning severity characters in place
apm audit --strip
```

### Reports

```bash
# SARIF to stdout (for GitHub Code Scanning upload)
apm audit -f sarif

# JSON to a file
apm audit -f json -o results.json

# Markdown for a GitHub Actions step summary
apm audit -f markdown -o "$GITHUB_STEP_SUMMARY"

# Auto-detect format from extension
apm audit -o report.sarif
```

### External scanners

```bash
# Invoke SkillSpector on PATH (offline by default)
apm audit --external skillspector

# Opt into LLM-powered analysis (needs an API key; makes network calls)
apm audit --external skillspector --external-llm

# Pass allowlisted scanner flags
apm audit --external skillspector --external-args "--model gpt-4o"

# Ingest a SARIF file from any scanner
apm audit --external sarif --external-sarif report.sarif
```

### CI gate

```bash
# Default CI gate (auto-discovers org policy)
apm audit --ci

# CI gate, baseline checks only (no policy)
apm audit --ci --no-policy

# CI gate with an explicit policy source
apm audit --ci --policy org
apm audit --ci --policy ./apm-policy.yml

# Full diagnostic report (don't stop at first failure)
apm audit --ci --no-fail-fast

# CI gate as JSON or SARIF
apm audit --ci -f json
apm audit --ci -f sarif -o audit.sarif
```

For the full workflow, see [Enforce in CI](../../../enterprise/enforce-in-ci/).

## Behavior

### Severity levels (content scan)

| Severity | Examples | Effect |
|---|---|---|
| Critical | Tag characters (U+E0001-E007F), bidi overrides (U+202A-E, U+2066-9), variation selectors 17-256 (U+E0100-E01EF, the Glassworm vector) | Exit `1`. Removed by `--strip`. Blocks `apm install` / `apm compile` / `apm unpack` by default. |
| Warning | Zero-width spaces and joiners (U+200B-D), variation selectors 1-15 (U+FE00-FE0E), bidi marks (U+200E-F, U+061C), invisible operators (U+2061-4), annotation markers (U+FFF9-B), deprecated formatting (U+206A-F), soft hyphen (U+00AD), mid-file BOM | Exit `2` if no critical findings. Removed by `--strip`. |
| Info | Non-breaking and unusual whitespace, emoji presentation selector (U+FE0F), ZWJ between emoji characters | Exit `0`. Shown only with `--verbose`. Preserved by `--strip`. |

### Drift detection

The default audit replays the install pipeline into a scratch tree and diffs the result against the working tree. It catches hand-edits to deployed files, missing integrations from a skipped `apm install`, and orphaned files. Drift is whole-project only; `--file` and explicit `PACKAGE` runs skip it. Use `--no-drift` to opt out (not recommended outside performance-constrained CI loops).

### CI checks (`--ci`)

`--ci` runs the baseline lockfile consistency checks defined in `src/apm_cli/policy/ci_checks.py`: lockfile presence, ref consistency, deployed-files presence, no orphaned packages, skill-subset consistency, MCP config consistency, content integrity (Unicode plus per-file SHA-256 hash drift on every deployed file, including local `.apm/` content via the synthesized self-entry), and an advisory `includes` consent check. Drift detection runs alongside and contributes to the exit code unless `--no-drift` is set. With policy discovery active, declared policy rules are evaluated against the resolved manifest.

### Mutual exclusions

- `--no-drift` cannot be combined with `--strip` or `--file`.
- `--ci` cannot be combined with `--strip`, `--dry-run`, `--file`, or `PACKAGE`.
- `--ci` does not support `--format markdown`.
- `--external` cannot be combined with `--strip`, `--dry-run`, or `--ci`; `--external-sarif` requires `--external sarif`; `--external-llm` / `--no-external-llm` and `--external-args` require `--external`.

## Exit codes

### Content scan mode

| Code | Meaning |
|---|---|
| `0` | Clean, info-only findings, drift-only (advisory) in bare audit, or successful `--strip`. |
| `1` | Critical findings detected. |
| `2` | Warning-only findings, or usage error (mutually exclusive flags). |
| `3` | Configuration or infrastructure error (feature not enabled, scanner not found, malformed SARIF). |

### CI gate mode (`--ci`)

| Code | Meaning |
|---|---|
| `0` | All checks passed. |
| `1` | One or more checks failed (including drift, hash drift, or policy violations). |

## Related

- [`apm install`](../install/) -- the built-in scan that blocks critical findings before deployment.
- [Drift and secure by default](../../../consumer/drift-and-secure-by-default/) -- consumer-side overview of the two-layer security model.
- [Enforce in CI](../../../enterprise/enforce-in-ci/) -- wiring `apm audit --ci` into branch protection.
