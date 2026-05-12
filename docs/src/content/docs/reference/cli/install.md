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

`PACKAGE_REF` accepts: shorthand (`owner/repo`), HTTPS or SSH Git URLs, FQDN shorthand (`host/owner/repo`), local paths (`./path`, `/abs/path`, `~/path`), packed bundles (`./bundle.tar.gz`), and marketplace refs (`NAME@MARKETPLACE[#ref]`).

:::caution
`http://` dependencies are refused unless you pass `--allow-insecure` (direct) or `--allow-insecure-host HOSTNAME` (transitive).
:::

## Options

### Common

| Flag | Default | Description |
|---|---|---|
| `--update` | off | Re-resolve dependencies to the latest Git ref allowed by `apm.yml` and rewrite `apm.lock.yaml`. Mutually exclusive with `--frozen`. Prefer the dedicated [`apm update`](../update/) command for the consent-gated workflow. |
| `--frozen` | off | Lockfile-only install: refuse to resolve anything new and fail if `apm.yml` and `apm.lock.yaml` have drifted. Mirrors `npm ci`. Mutually exclusive with `--update`. |
| `--dry-run` | off | Print the install plan without touching the filesystem. |
| `--force` | off | Overwrite locally-authored files on collision **and** bypass the security scan's critical-finding block. Does **not** refresh remote refs -- use `apm update` for that. Use only after independent verification. |
| `--verbose`, `-v` | off | Show per-file paths and full error context in the diagnostic summary. |
| `--dev` | off | Add new packages to `devDependencies`. Dev deps install locally but are excluded from `apm pack` output. |

### Target selection

| Flag | Default | Description |
|---|---|---|
| `--target`, `-t VALUE` | auto-detect | Force deployment targets. Comma-separated for multiple (`-t claude,cursor`). Values: `copilot`, `claude`, `cursor`, `opencode`, `codex`, `gemini`, `windsurf`, `agent-skills`, `all`. `all` expands to every harness above except `agent-skills`; combine `all,agent-skills` for both. Highest precedence in the chain `--target` > `apm.yml targets:` > auto-detect. With nothing to detect, install exits `2` with a teaching message. |
| `--runtime VALUE` | unset | Legacy alias for `--target` (single value only). Still accepted; prefer `--target`. |
| `--exclude VALUE` | unset | Skip a single runtime that auto-detect or `targets:` would otherwise enable. |
| `--only apm\|mcp` | both | Install only APM packages or only MCP servers. |
| `-g`, `--global` | off | Install to user scope (`~/.apm/`) instead of the current project. MCP servers deploy only to global-capable runtimes (Copilot CLI, Codex CLI). |
| `--legacy-skill-paths` | off | Deploy skills to per-client paths (`.cursor/skills/`, `.github/skills/`, ...) instead of the converged `.agents/skills/`. Env: `APM_LEGACY_SKILL_PATHS=1`. |

### Policy and trust

| Flag | Default | Description |
|---|---|---|
| `--no-policy` | off | Skip org policy enforcement for this invocation. Loudly logged. Does not bypass `apm audit --ci`. Env: `APM_POLICY_DISABLE=1`. |
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
| `--skill NAME` | all | Install only named skill(s) from a `SKILL_BUNDLE` package. Repeatable. The selection is persisted to `apm.yml` and `apm.lock.yaml`. Use `--skill '*'` to reset and install all. |
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
- **Diff-aware.** Packages whose ref or version changed in `apm.yml` are re-downloaded automatically; `--update` is only needed to pull a newer ref under a floating constraint. MCP servers with matching config are skipped (`already configured`); changed config is re-applied (`updated`).
- **No-op nudge.** When the lockfile is already satisfied and nothing needs deploying, install prints `[i] Run 'apm update' to check for newer versions.` so you know the silent success was not a missed refresh.
- **Frozen mode.** With `--frozen`, install resolves only what is in `apm.lock.yaml`. A direct dependency missing from the lockfile, or a missing lockfile entirely, exits `1`. Orphan lockfile entries (locked but no longer in `apm.yml`) are tolerated; local-path deps are skipped. This is a structural check, not a content check -- run `apm audit --ci` for hash verification.
- **Local `.apm/` deployment.** After dependencies are integrated, primitives in the project's own `.apm/` directory are deployed to the same targets. Local files win on collision. Skipped at `--global` and with `--only mcp`.
- **Stale-file cleanup.** Files a still-present package previously deployed but no longer produces are removed from the workspace, gated by per-file content hashes recorded in the lockfile (user-edited files are kept with a warning).
- **Security scan.** Source files are scanned for hidden Unicode and other tag-character / bidi-override patterns before deployment. Critical findings block the package; the install exits `1`. Use `--force` to deploy anyway, or run `apm audit --strip` first to remediate.
- **Diagnostic summary.** Output is grouped at the end (collisions, replacements, warnings, errors) instead of inline. Use `--verbose` to expand individual file paths.

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

### Install a local bundle produced by `apm pack`

```bash
apm install ./build/my-bundle
apm install ./my-bundle.tar.gz --as custom-name
apm install ./my-bundle --target opencode
```

### Install only a subset of skills from a bundle

```bash
apm install owner/skill-bundle --skill review --skill refactor
apm install owner/skill-bundle --skill '*'   # reset to all skills
```

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success. All requested dependencies and local content deployed. |
| `1` | Install failure: security scan blocked a critical finding, auth error, manifest write error, dependency resolution error, `--frozen` with a missing lockfile or a direct dependency absent from `apm.lock.yaml`, or unhandled exception. The diagnostic summary names the cause. |
| `2` | Usage error: no deployment target detectable (no `--target`, no `targets:` in `apm.yml`, no harness signal in the project), `--ssh` and `--https` both passed, `--frozen` and `--update` both passed, or a Click flag conflict. |

## Notes

- **`--force` is dual-purpose.** It overwrites locally-authored files on collision **and** disables the critical-finding block from the built-in security scan. It does **not** refresh remote refs -- for routine ref updates, run [`apm update`](../update/). To remediate findings, prefer `apm audit --strip`. See [Drift and secure by default](../../../consumer/drift-and-secure-by-default/).
- **Claude target prompt rewrite.** When deploying to `.claude/commands/`, prompt files with an `input:` front-matter key are rewritten to Claude's `arguments:` shape and `${input:name}` placeholders become `$name`. Argument names must match `^[A-Za-z][\w-]{0,63}$`; rejected names are dropped with a warning.
- **Copilot CLI env-var passthrough.** When deploying MCP entries to `~/.copilot/mcp-config.json`, `${env:VAR}` and `<VAR>` placeholders are translated to `${VAR}` so Copilot CLI resolves them at server-start. Plaintext secrets are never written to disk. Other targets currently resolve placeholders at install time.

## Related

- [`apm update`](../update/) -- refresh dependencies in `apm.yml` to their latest matching refs, with a consent gate.
- [`apm self-update`](../self-update/) -- upgrade the `apm` CLI binary itself.
- [`apm prune`](../prune/) -- remove orphaned packages and stale files.
- [`apm audit`](../audit/) -- explicit security reporting and remediation after install.
- [`apm targets`](../targets/) -- print which harnesses APM detects in the current directory.
- [Install packages (consumer guide)](../../../consumer/install-packages/) -- task-oriented walkthrough.
- [Manifest schema](../../manifest-schema/) -- field reference for `apm.yml`.
- [Lockfile spec](../../lockfile-spec/) -- field reference for `apm.lock.yaml`.
