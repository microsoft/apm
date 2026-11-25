# Core Workflow

## 5-step workflow

Install APM once: on macOS with Homebrew, use `brew install apm` (no tap).
For Linux/macOS without Homebrew or Windows, choose an option in
[Installation](./installation.md). Homebrew users update with `brew upgrade apm`.

```bash
# 1. Verify installation
apm --version

# 2. Initialize project
apm init my-project && cd my-project           # new project
cd existing-repo && apm init                   # existing repo

# 3. Install packages
apm install microsoft/apm-sample-package#v1.0.0

# 4. Compile root context (recommended for non-Copilot targets)
apm compile

# 5. Commit and share
git add apm.yml apm.lock.yaml .gitignore     # add .apm/ if you author primitives
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
  target:      <enum>                      # deprecated and ignored -- delete; use top-level targets:
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
| `.gitignore` | Yes | `apm install` adds `apm_modules/` to it |
| `.apm/` | Yes, if present | Local primitives you author (instructions, agents, etc.) |
| Deployed files: `.github/`, `.claude/`, `.agents/`, `.grok/`, root `.mcp.json`, `AGENTS.md`, `CLAUDE.md` (imports `AGENTS.md`) | Optional | Any clone rebuilds them from the lockfile (`apm install --frozen`). Commit them when something reads the repo without running APM, such as a cloud coding agent |
| `apm_modules/` | **No** | Installed dependencies -- gitignored automatically; restored by `apm install` |

## Team member setup

```bash
git clone <repo-url>
cd <repo>
apm install            # restores all deps from lockfile
```

Committed agent files can be reviewed and discovered after clone, but linked
package context requires `apm install`. Keep `apm_modules/` resources
available while agents or tools use them, even after APM exits. Native plugins
load from the committed `.agents/plugins/` and need no install.

`apm install` also deploys local `.apm/` content, which wins collisions,
even with no dependencies. For version replay and updates, see
[What the lockfile pins](dependencies.md#what-the-lockfile-pins).

## Local bundle install

`apm install <bundle>` accepts a directory, `.zip` (default), or legacy `.tar.gz` produced by `apm pack` and deploys its contents into the consumer's resolved target. Bundles are target-agnostic; the project decides where files land (same precedence as registry installs: `--target` > `apm.yml` > directory detection). Targets without native instruction deployment (OpenCode, Codex, and Gemini) stage instructions under `apm_modules/<slug>/.apm/instructions/` and print a hint to run `apm compile`. Grok Build deploys native `.grok/rules/`; compile it separately when you also want `AGENTS.md`.
