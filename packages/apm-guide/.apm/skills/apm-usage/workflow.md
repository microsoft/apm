# Core Workflow

## 5-step workflow

```bash
# 1. Install APM (one-time)
curl -sSL https://aka.ms/apm-unix | sh        # or irm on Windows

# 2. Initialize project
apm init my-project && cd my-project           # new project
cd existing-repo && apm init                   # existing repo

# 3. Install packages
apm install microsoft/apm-sample-package#v1.0.0

# 4. Compile root context (recommended for non-Copilot targets)
apm compile

# 5. Commit and share
git add apm.yml apm.lock.yaml .apm/ .github/ .claude/ .cursor/ .grok/ AGENTS.md
git commit -m "Add APM dependencies"
```

## apm.yml schema overview

```yaml
name:          <string>                    # REQUIRED -- package identifier
version:       <string>                    # REQUIRED -- semver (e.g. 1.0.0)
description:   <string>                    # optional
author:        <string>                    # optional
license:       <string>                    # optional -- SPDX (e.g. MIT)
targets:       <list<enum>>                  # optional -- canonical; includes copilot|claude|grok-build|...
target:        <string | list>               # legacy compatibility; prefer targets:
type:          <enum>                      # optional -- instructions|skill|hybrid|prompts
scripts:       <map<string, string>>       # optional -- named commands
dependencies:
  apm:         <list<ApmDependency>>       # optional
  mcp:         <list<McpDependency>>       # optional
devDependencies:                           # optional -- excluded from bundles
  apm:         <list<ApmDependency>>
  mcp:         <list<McpDependency>>
compilation:                               # optional
  target:      <enum>                      # copilot|claude|grok-build|codex|opencode|all (or list)
  strategy:    <enum>                      # distributed|single-file
  output:      <string>                    # custom output path
  chatmode:    <string>                    # chatmode to prepend
  resolve_links: <bool>                    # resolve markdown links (default true)
  source_attribution: <bool>              # include cosmetic source, version, and footer annotations for all targets (default: false; opt-in)
```

### Type behavior

| Value | Behavior |
|-------|----------|
| `instructions` | Compiled into AGENTS.md only; no skill directory |
| `skill` | Installed as skill only; no AGENTS.md |
| `hybrid` | Both AGENTS.md + skill installation |
| `prompts` | Commands/prompts only; no instructions/skills |

### Target auto-detection

When no target is specified, APM auto-detects from project signals. Prefer the
canonical `targets:` list:

```yaml
# Only these targets are compiled/installed
targets: [claude, copilot]
```

CLI equivalent: `--target claude,copilot` (comma-separated).

Signals include recognized `.github/` Copilot files and subdirectories for
`copilot`, `.claude/` or `CLAUDE.md` for `claude`, `.grok/` for stable
`grok-build`, `.cursor/` or legacy `.cursorrules` for `cursor`, `.codex/` for
`codex`, and `.gemini/` or `GEMINI.md` for `gemini`. OpenCode, Windsurf, and
Kiro use `.opencode/`, `.windsurf/`, and `.kiro/`.

Auto-detection applies only when both manifest fields are omitted. Prefer
canonical stable names. The legacy singular `target:` field also accepts CLI
aliases; experimental targets such as `grok-cloud` cannot be stored in
`targets:`.

| Input | Result |
|-------|--------|
| `target: bogus` (unknown token) | parse error -- fix the typo |
| `target: ""` or `target: []` (empty) | parse error -- remove the field to auto-detect |
| `target: [all, claude]` (`all` mixed with other targets) | parse error -- use `all` alone |
| `target: opencode,claude,copilot` (CSV string in YAML) | accepted; parses identically to the list form |
| Both fields omitted | auto-detect from the signals above |

## What to commit

| Path | Commit? | Why |
|------|---------|-----|
| `apm.yml` | Yes | Manifest -- declares dependencies |
| `apm.lock.yaml` | Yes | Lockfile -- pins exact commits for reproducibility |
| `.apm/` | Yes | Local primitives (instructions, agents, etc.) |
| Target-owned directories such as `.github/`, `.claude/`, `.grok/`, and `.agents/` | Yes | Deployed files for agent runtimes |
| `AGENTS.md` | Yes | Compiled root context for agents-family targets |
| `apm_modules/` | **No** | Downloaded sources -- add to `.gitignore` |

## Team member setup

```bash
git clone <repo-url>
cd <repo>
apm install            # restores all deps from lockfile
```

The lockfile ensures every team member gets the exact same dependency versions.
`apm install` also deploys the project's own `.apm/` content (instructions, prompts, agents, skills, hooks, commands) to target directories alongside dependency content. Local content wins on collision. This works even with zero dependencies.
Subsequent `apm install` reads locked commit SHAs for reproducible installs.
Use `apm install --update` to refresh to latest refs.

## Local bundle install

`apm install <bundle>` accepts a directory, `.zip` (default), or legacy `.tar.gz` produced by `apm pack` and deploys its contents into the consumer's resolved target. Bundles are target-agnostic; the project decides where files land (same precedence as registry installs: `--target` > `apm.yml` > directory detection). Targets without native instruction deployment (OpenCode, Codex, and Gemini) stage instructions under `apm_modules/<slug>/.apm/instructions/` and print a hint to run `apm compile`. Grok Build deploys native `.grok/rules/`; compile it separately when you also want `AGENTS.md`.
