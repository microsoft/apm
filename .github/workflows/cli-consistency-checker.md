---
name: CLI Consistency Checker
description: Inspects the APM CLI to identify inconsistencies, typos, bugs, or documentation gaps by running commands and analyzing output
on:
  # Keep activation read-only while retaining compiler revocation checks.
  report-blocked-version: false
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
    labels: [type/automation, area/cli]

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

Do NOT use a hand-written command list; it goes stale as commands are added. Derive the command surface from `apm --help` itself by walking every command group recursively. A group lists its subcommands under a `Commands:` heading (some groups split them into headings such as `Consumer commands:` and `Authoring commands:`). Run this walker and save all output for analysis:

```bash
apm_subcommands() {
  apm "$@" --help | awk '/^([A-Za-z]+ )*[Cc]ommands:$/ { in_cmds = 1; next } /^$/ { in_cmds = 0 } in_cmds && /^  [a-z]/ { print $1 }'
}
apm_help_walk() {
  echo "===== apm $* --help"
  apm "$@" --help
  for sub in $(apm_subcommands "$@"); do
    apm_help_walk "$@" "$sub"
  done
}
mkdir -p /tmp/gh-aw/agent
apm_help_walk > /tmp/gh-aw/agent/apm-help-all.txt 2>&1
grep '^===== ' /tmp/gh-aw/agent/apm-help-all.txt
```

Every top-level command and every subcommand at any depth gets its own `--help` section in `/tmp/gh-aw/agent/apm-help-all.txt`. Do not skip any of them. Record the total number of commands inspected (`grep -c '^===== ' /tmp/gh-aw/agent/apm-help-all.txt`).

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
The CLI reference lives in `docs/src/content/docs/reference/cli/`, one page per top-level command (`<command>.md`); subcommands at any depth are documented on their top-level command's page (for example, `apm marketplace package add` lives in `marketplace.md`). Read the documentation files:

```
ls docs/src/content/docs/reference/cli/
```

Then read each page one command at a time while comparing it with that command's help output from Step 2 (for example `cat docs/src/content/docs/reference/cli/install.md`).

```
cat README.md
```

Then compare:
- Every top-level command found in Step 2 must have a page in `docs/src/content/docs/reference/cli/`, and every page there must match a command that exists in the actual CLI.
- Every subcommand found in Step 2 must be documented on its top-level command's page, and every subcommand documented there must exist in the actual CLI.
- Every command mentioned in `README.md` must exist in the actual CLI.
- Flag names, argument names, and descriptions should match between docs and CLI help.
- Usage examples in docs should be syntactically correct per the actual CLI interface.
- If docs mention commands or flags not present in the CLI (or vice versa), flag as **high severity**.
- Hidden aliases and hidden options (for example `apm info`) do not appear in `--help`, so Step 2 does not list them. Before flagging a command or flag that the docs mention but Step 2 did not find, invoke it directly (for example `apm info --help`); report it only if that invocation fails.

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
