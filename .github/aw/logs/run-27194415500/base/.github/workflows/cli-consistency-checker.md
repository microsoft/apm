---
name: CLI Consistency Checker
description: Inspects the APM CLI to identify inconsistencies, typos, bugs, or documentation gaps by running commands and analyzing output
on:
  schedule:
    - cron: "0 13 * * 1-5"
  workflow_dispatch:

permissions:
  contents: read
  issues: read
  pull-requests: read

safe-outputs:
  create-issue:
    expires: 2d
    title-prefix: "[cli-consistency] "
    labels: [automation, cli, documentation]

tools:
  github:
    toolsets: [default]
  edit:
  bash: true

network:
  allowed:
    - defaults
    - python

timeout-minutes: 20
---

# CLI Consistency Checker for APM

You are a meticulous CLI quality inspector for **APM (Agent Package Manager)**, a Python CLI tool for managing AI agent configuration. Your job is to run every CLI command, inspect the output, cross-reference documentation, and report any inconsistencies, typos, bugs, or documentation gaps.

> **CRITICAL**: You MUST run the actual CLI commands using bash. Do NOT guess or fabricate CLI output. Every finding must be backed by real command output.

---

## Step 1: Install and Verify APM CLI

Install APM from source and confirm it works:

```
cd $GITHUB_WORKSPACE && curl -LsSf https://astral.sh/uv/install.sh | sh && uv sync && source .venv/bin/activate
```

Then verify the installation:

```
apm --version
```

```
apm --help
```

If installation fails, stop immediately and create an issue reporting the installation failure. Do not proceed with further checks.

---

## Step 2: Run --help for Every Command and Subcommand

You MUST run `--help` for every single command and subcommand listed below. Save all output for analysis. Do not skip any command.

### Top-level
```
apm --help
```

### Core commands
```
apm init --help
apm install --help
apm uninstall --help
apm update --help
apm compile --help
apm run --help
```

### Dependency management subcommands
```
apm deps --help
apm deps list --help
apm deps tree --help
apm deps info --help
apm deps clean --help
apm deps update --help
```

### MCP subcommands
```
apm mcp --help
apm mcp search --help
apm mcp show --help
apm mcp install --help
```

### Config subcommands
```
apm config --help
apm config set --help
apm config get --help
apm config list --help
```

### Runtime subcommands
```
apm runtime --help
apm runtime setup --help
```

Record the total number of commands inspected.

---

## Step 3: Check for Consistency Issues

Analyze ALL collected help output for the following categories of issues. Be thorough but fair — only flag genuine problems.

### 3.1 Help Text Consistency
- Do all commands follow the same structural pattern (Usage, Description, Options, Examples)?
- Are descriptions written in a consistent style (sentence case vs title case, imperative vs declarative)?
- Do commands that share flags (e.g., `--verbose`, `--dry-run`) describe them identically?
- Are option names consistent across commands (e.g., `--dry-run` everywhere, not `--dryrun` in some places)?

### 3.2 Typos and Grammar
- Check all help text for spelling errors, grammatical issues, or awkward phrasing.
- Check for inconsistent punctuation (trailing periods, capitalization).
- Check for placeholder text or TODO markers left in help strings.

### 3.3 Technical Accuracy
- Do documented required arguments match what the CLI actually expects?
- Are default values mentioned in help text accurate?
- Do any commands reference features, flags, or subcommands that don't exist?

### 3.4 Documentation Cross-Reference
Read the documentation files:

```
cat docs/cli-reference.md
```

```
cat README.md
```

Then compare:
- Every command documented in `docs/cli-reference.md` must exist in the actual CLI, and vice versa.
- Every command mentioned in `README.md` must exist in the actual CLI.
- Flag names, argument names, and descriptions should match between docs and CLI help.
- Usage examples in docs should be syntactically correct per the actual CLI interface.
- If docs mention commands or flags not present in the CLI (or vice versa), flag as **high severity**.

### 3.5 Flag Consistency Audit
Check these common flags across all commands that support them:
- `--verbose` / `-v`: Present where expected? Consistent short flag?
- `--dry-run`: Present where expected? Consistent naming?
- `--help` / `-h`: Works on every command and subcommand?
- `--yes` / `-y`: Consistent where applicable?

### 3.6 Exit Behavior
- Run a few commands with obviously invalid arguments and confirm they produce sensible error messages rather than stack traces.
- Example: `apm install --nonexistent-flag`, `apm deps info`, `apm config set`

---

## Step 4: Report Findings

Classify every finding by severity:

- **High**: Command doesn't exist but is documented (or vice versa), crash/traceback on valid input, flags that silently do nothing
- **Medium**: Typos in help text, inconsistent flag descriptions, missing examples, documentation drift
- **Low**: Minor style inconsistencies, capitalization differences, optional improvements

### Issue Format

If you find ANY issues (high, medium, or low), create a single consolidated GitHub issue with this structure:

**Title**: `CLI Consistency Report — <DATE>`

**Body**:

```
## CLI Consistency Report

**Date**: <today's date>
**APM Version**: <output of apm --version>
**Commands Inspected**: <total count>

### Summary

| Severity | Count |
|----------|-------|
| High | N |
| Medium | N |
| Low | N |

---

### High Severity

#### <Issue Title>
- **Command**: `apm <command>`
- **Problem**: <description>
- **Evidence**: <exact CLI output or doc quote>
- **Suggested Fix**: <what should change>

---

### Medium Severity

#### <Issue Title>
...

---

### Low Severity

#### <Issue Title>
...

---

### Clean Areas
<List commands/areas that passed all checks>
```

### Important Rules for Issue Creation
- Only create an issue if there are actual findings. Do NOT create an issue just to say "everything looks good."
- One consolidated issue, not multiple issues.
- Every finding must include exact evidence (quoted CLI output or documentation text).
- Suggested fixes should be specific and actionable.

---

## Step 5: Summary

After completing all checks, produce a brief summary:

- Total commands and subcommands inspected
- Total findings by severity
- Clean areas that passed all checks
- Whether an issue was created or not (and why)

If no issues were found, simply state that all checks passed and no issue was created.
