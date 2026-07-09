---
title: apm install
description: Install dependencies and deploy primitives to detected targets.
sidebar:
  order: 2
---

## Synopsis

```bash
apm install [PACKAGE_REF...] [OPTIONS]
```

## Description

`apm install` resolves the dependencies declared in `apm.yml`, downloads them (with transitive resolution and a content-addressed cache), runs the built-in security scan, and deploys the resulting primitives plus the project's own `.apm/` content into every harness target it detects. It writes `apm.lock.yaml` so the next install on any machine reproduces the same files.

With no arguments it installs everything from `apm.yml`. With one or more `PACKAGE_REF` arguments it adds those packages to `apm.yml` (creating one if needed) and installs only what was added. `apm install --mcp NAME` is the dedicated path for adding an MCP server entry.

`PACKAGE_REF` accepts: shorthand (`owner/repo`), HTTPS or SSH Git URLs, FQDN shorthand (`host/owner/repo`), local paths (`./path`, `/abs/path`, `~/path`), packed bundles (`./bundle.zip`, `./bundle.tar.gz`), and marketplace refs (`NAME@MARKETPLACE[#ref]`).

:::caution
`http://` dependencies are refused unless you pass `--allow-insecure` (direct) or `--allow-insecure-host HOSTNAME` (transitive).
:::

## Options

### Common

| Flag | Default | Description |
|---|---|---|
| `--update` | off | Re-resolve dependencies to the latest version or Git ref allowed by `apm.yml` and rewrite `apm.lock.yaml`. Mutually exclusive with `--frozen`. Prefer the dedicated [`apm update`](../update/) command for the consent-gated workflow. |
| `--frozen` | off | Lockfile-only install: refuse to resolve anything new and fail if `apm.yml` and `apm.lock.yaml` have drifted. Mirrors `npm ci`. Mutually exclusive with `--update`. |
| `--dry-run` | off | Print the install plan without touching the filesystem. |
| `--force` | off | Overwrite locally-authored files on collision **and** bypass the security scan's critical-finding block. Does **not** suppress general install errors (any reported error still exits `1`, matching npm / pip / cargo). Does **not** refresh remote refs -- use `apm update` for that. Use only after independent verification. |
| `--verbose`, `-v` | off | Show per-file paths and full error context in the diagnostic summary. |
| `--dev` | off | Add new packages to `devDependencies`. Dev deps install locally but are excluded from `apm pack` output. |

### Deploy location

| Flag | Default | Description |
|---|---|---|
| `--root DIR` | `$PWD` | Redirect every write -- `apm_modules/`, `apm.lock.yaml`, `.gitignore`, and integrated harness files -- under `DIR`, while `apm.yml`, `.apm/`, and local-path dependencies still resolve from the current working directory. Mirrors `pip install --target` / `npm install --prefix`. `DIR` is created if missing (except under `--dry-run`, which refuses to create it). Not valid with `--global` (user scope), which exits `2`. |

### Target selection

| Flag | Default | Description |
|---|---|---|
| `--target`, `-t VALUE` | auto-detect | Force deployment targets. Comma-separated for multiple (`-t claude,cursor`). Values: `copilot`, `claude`, `cursor`, `opencode`, `codex`, `gemini`, `antigravity`, `windsurf`, `kiro`, `intellij`, `vscode`, `agent-skills`, `all`; experimental `copilot-cowork` and `copilot-app` are also accepted when enabled. `intellij` is MCP-only and writes JetBrains Copilot's user-scope MCP config without deploying file-level primitives. `all` excludes `agent-skills`, `antigravity`, and `intellij`; combine them explicitly to add them. Highest precedence in the chain `--target` > `apm.yml targets:` > `apm config set target ...` > auto-detect. With nothing to detect, install exits `2` with a teaching message. |
| `--runtime VALUE` | unset | Legacy alias for `--target` (single value only). Still accepted; prefer `--target`. |
| `--exclude VALUE` | unset | Skip a single runtime that auto-detect or `targets:` would otherwise enable. |
| `--only apm\|mcp` | both | Install only APM packages or only MCP servers. |
| `-g`, `--global` | off | Install to user scope (`~/.apm/`) instead of the current project. MCP servers deploy only to global-capable runtimes, such as Copilot CLI, Claude Code, Codex CLI, Gemini CLI, Antigravity CLI, Kiro, Windsurf, and JetBrains Copilot. |
| `--legacy-skill-paths` | off | Deploy skills to per-client paths (`.cursor/skills/`, `.github/skills/`, ...) instead of the converged `.agents/skills/`. Env: `APM_LEGACY_SKILL_PATHS=1`. |

### Policy and trust

| Flag | Default | Description |
|---|---|---|
| `--no-policy` | off | Skip org policy enforcement for this invocation. Loudly logged. Does not bypass `apm audit --ci`. Env: `APM_POLICY_DISABLE=1`. |
| `--audit <off\|warn\|block>` | (config/policy) | Run a content audit over the files this install deploys. `warn` records findings in the summary; `block` halts the install on critical findings. Overrides your `audit-on-install` config but cannot relax an org policy floor. Requires the `external-scanners` experimental flag. |
| `--no-audit` | off | Disable the install-time audit for this invocation (equivalent to `--audit off`). Cannot relax an org policy `block` floor. |
| `--trust-transitive-mcp` | off | Trust self-defined MCP servers shipped by transitive packages without re-declaring them in your `apm.yml`. |
| `--allow-insecure` | off | Permit direct `http://` (non-TLS) dependencies. |
| `--allow-insecure-host HOSTNAME` | unset | Permit transitive `http://` dependencies from `HOSTNAME`. Repeatable. |

### Cache and network

| Flag | Default | Description |
|---|---|---|
| `--parallel-downloads N` | `4` | Max concurrent package downloads. `0` disables parallelism. |
| `--refresh` | off | Bypass the persistent cache and re-fetch every dependency from upstream. |
| `--ssh` | off | Prefer SSH transport for shorthand (`owner/repo`) deps. Mutually exclusive with `--https`. |
| `--https` | off | Prefer HTTPS transport for shorthand deps. Mutually exclusive with `--ssh`. |
| `--allow-protocol-fallback` | off | Restore the legacy permissive HTTPS<->SSH fallback chain. Env: `APM_ALLOW_PROTOCOL_FALLBACK=1`. |

Transport env vars: `APM_GIT_PROTOCOL` (`ssh` or `https`) sets the default initial transport for shorthand deps; `APM_ALLOW_PROTOCOL_FALLBACK=1` mirrors `--allow-protocol-fallback`.

### Skill subset

| Flag | Default | Description |
|---|---|---|
| `--skill NAME` | all | Install only named skill(s) from a skill collection (`SKILL_BUNDLE` or plugin manifest). Repeatable. For plugin manifests, `NAME` may be the skill name or manifest path, such as `skills/productivity/grill-me`. The selection is persisted to `apm.yml` and `apm.lock.yaml`. `--skill` is additive across separate installs: a later `apm install <bundle> --skill X` adds `X` to the existing pin (union) rather than replacing it -- previously deployed skills are never silently removed. Use `--skill '*'` to reset to the full bundle; to drop a single skill, edit the `skills:` list in `apm.yml` and re-run `apm install`. |
| `--as ALIAS` | bundle id | Override the log/display label for a local-bundle install. Only valid with a single local-bundle `PACKAGE_REF`. |

### MCP server entry (use only with `--mcp`)

| Flag | Default | Description |
|---|---|---|
| `--mcp NAME` | unset | Add an MCP server entry to `apm.yml` and install it. Pair with the flags below or pass an executable after `--`. |
| `--transport stdio\|http\|sse\|streamable-http` | inferred | Inferred from `--url` or the post-`--` argv when omitted. |
| `--url URL` | unset | Endpoint for `http`, `sse`, or `streamable-http` transports. Scheme must be `http` or `https`. |
| `--env KEY=VALUE` | unset | Environment variable for stdio MCP servers. Repeatable. |
| `--header KEY=VALUE` | unset | HTTP header for remote MCP servers. Repeatable. Requires `--url`. |
| `--mcp-version VER` | unset | Pin a registry MCP entry to a specific version. |
| `--registry URL` | `https://api.mcp.github.com` | Custom MCP registry URL for resolving `--mcp NAME`. Persisted to `apm.yml`. Overrides `MCP_REGISTRY_URL`. Not valid with `--url` or a stdio command. |

## Behavior

- **Auto-bootstrap.** `apm install <pkg>` with no `apm.yml` creates a minimal one. Bare `apm install` with no `apm.yml` exits with a hint to run `apm init` or `apm install <org/repo>`.
- **Target persistence on bootstrap.** When `--target` maps to recognized manifest targets, those target(s) are persisted to the new manifest's `targets:` field so a later bare `apm update` redeploys to the same targets without re-specifying `--target`.
- **Diff-aware.** Packages whose ref or version changed in `apm.yml` are re-downloaded automatically; `--update` is only needed to pull a newer ref under a floating constraint. MCP servers with matching config are skipped (`already configured`); changed config is re-applied (`updated`).
- **Lockfile replay.** For unchanged Git dependencies, install reuses the locked commit for the whole resolved graph, including transitive packages. Upstream changes to a transitive package's `apm.yml` are picked up only when you regenerate the graph, for example with `apm update` or `apm lock --update`. See the [lockfile specification](../../lockfile-spec/) for the replay contract.
- **Semver ranges on git deps.** `ref:` accepts semver ranges (`^1.2.0`, `~1.4`, `>=2.0 <3`, `1.5.x`) for git-source deps. APM runs `git ls-remote` against the dep, picks the highest tag matching the range, and pins the resolved tag plus commit SHA, version, and original constraint in `apm.lock.yaml`. Subsequent installs replay the lockfile without network; use `--update` (or change the manifest constraint) to re-resolve. See [manage dependencies](../../../consumer/manage-dependencies/#pin-a-semver-range) for the supported syntax.
- **No-op nudge.** When the lockfile is already satisfied and nothing needs deploying, install prints `[i] Run 'apm update' to check for newer versions.` so you know the silent success was not a missed refresh.
- **Frozen mode.** With `--frozen`, install resolves only what is in `apm.lock.yaml`. A direct dependency missing from the lockfile, or a missing lockfile entirely, exits `1`. Orphan lockfile entries (locked but no longer in `apm.yml`) are tolerated; local-path deps are skipped. This is a structural check, not a content check -- run `apm audit --ci` for hash verification.
- **Local `.apm/` deployment.** After dependencies are integrated, primitives in the project's own `.apm/` directory are deployed to the same targets. Local files win on collision. Skipped at `--global` and with `--only mcp`.
- **User-scope root context hint.** Compilation stays explicit. After `apm install -g`, targets with native user-scope instruction files pick up global instructions during install. Targets whose user-scope instruction surface is a root context file require [`apm compile --global`](../compile/#global-compilation); install prints a one-line `[i]` hint and writes no root context file.
- **Stale-file cleanup.** Files a still-present package previously deployed but no longer produces are removed from the workspace, gated by per-file content hashes recorded in the lockfile (user-edited files are kept with a warning).
- **Enterprise marketplace gate.** When installing from a `*.ghe.com` marketplace, bare cross-repo `repo:` fields (e.g. `repo: owner/repo`) are refused before any network request runs, preventing dependency-confusion attacks. Host-qualify the field to proceed: `repo: corp.ghe.com/owner/repo` for an enterprise dep, or `repo: github.com/owner/repo` for a declared cross-host dep.
- **Security scan.** Source files are scanned for hidden Unicode and other tag-character / bidi-override patterns before deployment. Critical findings block the package; the install exits `1`. Use `--force` to deploy anyway, or run `apm audit --strip` first to remediate.
- **Diagnostic summary.** Output is grouped at the end (collisions, replacements, warnings, errors) instead of inline. Use `--verbose` to expand individual file paths.
- **Default registry routing.** When a default registry is configured (project `registries.default` in `apm.yml` or `registry.<name>.default true` in `~/.apm/config.json`), unscoped `owner/repo#ref` shorthand deps passed to `apm install` route to the registry instead of GitHub. A `#<version>` selector is required; omitting it exits `1`. The selector may be a semver range (`^1.0.0`), an exact version (`1.2.3`), or a non-semver label (`main`, `stable`, `v1.4.2`) -- the registry exact-matches non-semver selectors against its published version list. GitHub probe is skipped for these deps; use the `git:` URL form in `apm.yml` to force the GitHub path (e.g., `- git: https://github.com/owner/repo.git`).

## Examples

### Install everything from apm.yml

```bash
apm install
```

### Install (and add) a specific package

```bash
apm install microsoft/apm-sample-package
apm install https://gitlab.com/acme/coding-standards.git
apm install code-review@acme-plugins#v2.0.0
```

### Install only an MCP server

```bash
# Stdio server via post-`--` argv
apm install --mcp filesystem -- npx -y @modelcontextprotocol/server-filesystem /workspace

# Registry entry
apm install --mcp io.github.github/github-mcp-server

# Remote HTTP server
apm install --mcp my-api --url https://mcp.example.com --header "Authorization=Bearer ${API_TOKEN}"
```

### Pick targets explicitly

```bash
apm install --target claude,cursor
apm install --target all,agent-skills
apm install --exclude codex
```

### Install in CI (no interactive prompts, no policy escape)

```bash
# Fail fast on any drift; never bypass policy in CI.
apm install --parallel-downloads 8
```

For a CI workflow that also gates on `apm audit --ci`, see [Enforce in CI](../../../enterprise/enforce-in-ci/).

### Preview without writing

```bash
apm install --dry-run
apm install microsoft/apm-sample-package --dry-run
```

### Redirect writes to a scratch directory

```bash
# Resolve apm.yml + local deps from this directory, but write
# apm_modules/, apm.lock.yaml, and harness files under /tmp/apm-out.
apm install --root /tmp/apm-out --target copilot

# The source tree stays clean; the deploy root holds every artifact.
ls /tmp/apm-out          # apm_modules/  apm.lock.yaml  .github/  .gitignore
```

### Install a local bundle produced by `apm pack`

```bash
apm install ./build/my-bundle
apm install ./my-bundle.zip --as custom-name
apm install ./my-bundle --target opencode
```

### Install only a subset of skills from a bundle

```bash
apm install owner/skill-bundle --skill review
apm install owner/skill-bundle --skill refactor   # adds refactor; review is kept (union)
apm install owner/skill-bundle --skill '*'         # reset to all skills
```

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success. All requested dependencies and local content deployed. |
| `1` | Install failure: security scan blocked a critical finding, auth error, manifest write error, dependency resolution error, `--frozen` with a missing lockfile or a direct dependency absent from `apm.lock.yaml`, any reported install error (the diagnostic summary closes with `Installation failed with N error(s)`), or unhandled exception. `--force` does **not** suppress general install errors. The diagnostic summary names the cause. |
| `2` | Usage error: no deployment target detectable (no `--target`, no `target(s):` in `apm.yml`, no default target configured via `apm config set target <value>`, and no harness signal in the project), `--ssh` and `--https` both passed, `--frozen` and `--update` both passed, `--root` combined with `--global`, or a Click flag conflict. |

## Notes

- **`--force` is dual-purpose.** It overwrites locally-authored files on collision **and** disables the critical-finding block from the built-in security scan. It does **not** suppress general install errors -- any error reported in the diagnostic summary still exits `1` (matches `npm` / `pip` / `cargo`). It does **not** refresh remote refs -- for routine ref updates, run [`apm update`](../update/). To remediate findings, prefer `apm audit --strip`. See [Drift and secure by default](../../../consumer/drift-and-secure-by-default/).
- **Claude target prompt rewrite.** When deploying to `.claude/commands/`, prompt files with an `input:` front-matter key are rewritten to Claude's `arguments:` shape and `${input:name}` placeholders become `$name`. Argument names must match `^[A-Za-z][\w-]{0,63}$`; rejected names are dropped with a warning.
- **MCP env-var passthrough.** Copilot CLI and Kiro translate `${env:VAR}` and `<VAR>` to `${VAR}` in their MCP configs. Kiro writes `.kiro/settings/mcp.json` and `~/.kiro/settings/mcp.json` with `0o600` permissions. JetBrains Copilot preserves env references as `${env:VAR}` in `github-copilot/intellij/mcp.json`. Plaintext secrets are never written to disk for these runtime-resolved targets; legacy targets resolve placeholders at install time.

### Install from a private registry (experimental)

Enable the feature, configure the registry (in `apm.yml` and/or `~/.apm/config.json`), and run install normally. APM resolves registry-sourced deps alongside git deps:

```bash
apm experimental enable registries

# Option A: apm.yml has a registries: block and registry-routed deps
apm install

# Option B: workstation config only (no registries: block in apm.yml)
apm config set registry.corp-main.url https://artifactory.corp.example.com/apm
apm config set registry.corp-main.token eyJ...
apm config set registry.corp-main.default true
apm install

# In CI: use env var for the token, never commit it
APM_REGISTRY_TOKEN_CORP_MAIN=eyJ... apm install --frozen
```

See [Registries](../../../guides/registries/) for the full setup guide.

## Related

- [`apm update`](../update/) -- refresh dependencies in `apm.yml` to their latest matching versions or refs, with a consent gate.
- [`apm self-update`](../self-update/) -- upgrade the `apm` CLI binary itself.
- [`apm prune`](../prune/) -- remove orphaned packages and stale files.
- [Registries](../../../guides/registries/) -- end-to-end guide for registry-sourced dependencies.
- [`apm audit`](../audit/) -- explicit security reporting and remediation after install.
- [`apm targets`](../targets/) -- print which harnesses APM detects in the current directory.
- [Install packages (consumer guide)](../../../consumer/install-packages/) -- task-oriented walkthrough.
- [Manifest schema](../../manifest-schema/) -- field reference for `apm.yml`.
- [Lockfile spec](../../lockfile-spec/) -- field reference for `apm.lock.yaml`.
