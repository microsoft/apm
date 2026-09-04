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
| `--update` | off | Re-resolve dependencies to the latest version or Git ref allowed by `apm.yml` and rewrite `apm.lock.yaml`. Mutable Git refs must resolve against upstream; APM does not fall back to stale refs from the local bare Git cache. Mutually exclusive with `--frozen`. For interactive use with a confirmation prompt, use [`apm update`](../update/) instead. |
| `--frozen` | off | Lockfile-only install: refuse to resolve anything new and fail before any project, config, deployment, or cache write if `apm.lock.yaml` is missing or out of sync with `apm.yml`, including MCP state. Mirrors `npm ci`. Mutually exclusive with `--update`, positional package additions, and `--mcp`. |
| `--dry-run` | off | Print the install plan without deployment writes. Positional packages and ref changes appear in the preview after validation but do not change an existing `apm.yml`. Project auto-bootstrap still keeps its new manifest and any explicit `--target` selection for the next run; global dry-run bootstrap uses temporary preview state and does not create `~/.apm`. The `-g --mcp` path creates no user manifest, lockfile, or runtime configuration. |
| `--force` | off | Overwrite locally-authored files on collision **and** bypass the security scan's critical-finding block. Does **not** suppress general install errors (any reported error still exits `1`, matching npm / pip / cargo) or select ref freshness. Add `--update` or `--refresh` to resolve mutable refs upstream; [`apm update`](../update/) does so with or without `--force`. Use only after independent verification. |
| `--verbose`, `-v` | off | Show per-file paths and full error context in the diagnostic summary. |
| `--dev` | off | Add new packages to `devDependencies`. Dev deps install locally but are excluded from `apm pack` output. |

### Deploy location

| Flag | Default | Description |
|---|---|---|
| `--root DIR` | `$PWD` | Redirect every write -- `apm_modules/`, `apm.lock.yaml`, `.gitignore`, and integrated harness files -- under `DIR`, while `apm.yml`, `.apm/`, and local-path dependencies still resolve from the current working directory. Mirrors `pip install --target` / `npm install --prefix`. `DIR` is created if missing (except under `--dry-run`, which refuses to create it). Not valid with `--global` (user scope), which exits `2`. |

### Target selection

| Flag | Default | Description |
|---|---|---|
| `--target`, `-t VALUE` | auto-detect | Force deployment targets. Comma-separated for multiple (`-t claude,cursor`). Values: `copilot`, `claude`, `grok-build`, `cursor`, `opencode`, `codex`, `gemini`, `antigravity`, `windsurf`, `kiro`, `intellij`, `vscode`, `agent-skills`, `hermes`, `all`; experimental `copilot-cowork`, `copilot-app`, and `grok-cloud` (skills only) are also accepted when enabled. Hermes is stable but explicit-only. IntelliJ-specific integration is MCP-only and writes JetBrains Copilot's user-scope MCP config; package file primitives use the Copilot profile. `all` excludes `agent-skills`, `antigravity`, `hermes`, `intellij`, and all experimental targets; combine them explicitly to add them, for example `all,hermes`. Explicit MCP target lists are exact: `intellij,claude` writes only those two MCP configs. See the precedence note below. With nothing to detect, install exits `2` with a teaching message. |
| `--runtime VALUE` | unset | Legacy alias for `--target` (single value only). Still accepted; prefer `--target`. |
| `--exclude VALUE` | unset | Skip one runtime from the resolved MCP/LSP target set (explicit selection, manifest, saved config, or auto-detection). |
| `--only apm\|mcp` | both | Install only APM packages or MCP/LSP service dependencies. Use `--only=apm` to skip service configuration and `--only=mcp` to select MCP and LSP services only. |
| `-g`, `--global` | off | Install to user scope (`~/.apm/`) instead of the current project. `apm install -g --mcp NAME` creates or updates `~/.apm/apm.yml`, then deploys only to global-capable runtimes, such as Copilot CLI, Claude Code, Codex CLI, Gemini CLI, Antigravity CLI, Hermes, Kiro, Windsurf, and JetBrains Copilot. Mixed selections skip workspace-only targets with a warning. A selection with no global-capable target exits `2` before changing the user manifest, lockfile, or runtime configuration. |
| `--legacy-skill-paths` | off | Deploy skills to per-client paths (`.cursor/skills/`, `.github/skills/`, ...) instead of the converged `.agents/skills/`. Env: `APM_LEGACY_SKILL_PATHS=1`. |

File primitives resolve targets in this order: `--target`, manifest
`targets:`, `apm config set target ...`, then auto-detection. MCP resolves
`--runtime` / `--target`, then manifest targets, saved config, then
auto-detection only when `apm.yml` declares no targets.

Native [Agent Plugin registration](../../../consumer/copilot-agent-plugins/#requirements)
does not discover or execute the Copilot CLI. APM owns deterministic
materialization and settings projection; you provide a supported runtime when
you use the generated registration.

### Policy and trust

| Flag | Default | Description |
|---|---|---|
| `--no-policy` | off | Skip org policy enforcement for this invocation. Loudly logged. Does not bypass `apm audit --ci`. Env: `APM_POLICY_DISABLE=1`. |
| `--audit <off\|warn\|block>` | (config/policy) | Run a content audit over the files this install deploys. `warn` records findings in the summary; `block` halts the install on critical findings. Overrides your `audit-on-install` config but cannot relax an org policy floor. Requires the `external-scanners` experimental flag. |
| `--no-audit` | off | Disable the install-time audit for this invocation (equivalent to `--audit off`). Cannot relax an org policy `block` floor. |
| `--trust-transitive-mcp` | off | Trust self-defined MCP servers shipped by transitive packages without re-declaring them in your `apm.yml`. |
| `--trust-bin` / `--no-trust-bin` | (warn) | Per-invocation consent for marketplace-plugin `bin/` executable deployment. `--trust-bin` explicitly approves deployment and suppresses the trust-posture warning. `--no-trust-bin` skips `bin/` deployment even when policy permits it. Default (neither flag): deploys `bin/` and emits a warning. In non-interactive contexts (piped output or `--frozen`), the default is equivalent to `--no-trust-bin`. The `allowExecutables` policy gate always takes precedence; `--trust-bin` cannot override a policy-level deny. For persistent per-package trust, use [`apm approve`](../approve/). |
| `--allow-insecure` | off | Permit direct `http://` (non-TLS) dependencies. |
| `--allow-insecure-host HOSTNAME` | unset | Permit transitive `http://` dependencies from `HOSTNAME`. Repeatable. |

### Cache and network

| Flag | Default | Description |
|---|---|---|
| `--parallel-downloads N` | `4` | Max concurrent package downloads. `0` disables parallelism. |
| `--refresh` | off | Re-resolve dependency refs against upstream and bypass cached content. Unlike `--update`, which may reuse content at a freshly resolved SHA, `--refresh` fetches it again. |
| `--ssh` | off | Prefer SSH transport for shorthand (`owner/repo`) deps. Mutually exclusive with `--https`. |
| `--https` | off | Prefer HTTPS transport for shorthand deps. Mutually exclusive with `--ssh`. |
| `--allow-protocol-fallback` | off | Restore the legacy permissive HTTPS<->SSH fallback chain. Env: `APM_ALLOW_PROTOCOL_FALLBACK=1`. |

Transport env vars: `APM_GIT_PROTOCOL` (`ssh` or `https`) sets the default initial transport for shorthand deps; `APM_ALLOW_PROTOCOL_FALLBACK=1` mirrors `--allow-protocol-fallback`.

### Skill subset

| Flag | Default | Description |
|---|---|---|
| `--skill NAME` | all | Install only named skills from a dependency that exposes selectable skills. Applies to both git-longhand and registry-longhand (`id:`/`registry:`) dependencies. Repeatable. For plugin manifests, `NAME` may be the skill name or manifest path, such as `skills/productivity/grill-me`. A CLI name that matches no declared skill is an install error; the diagnostic lists the available names. If a previously persisted `skills:` pin later matches no available source skill, install stays successful but warns with the package, requested names, and available names instead of silently doing nothing. The selection is persisted to `apm.yml` and `apm.lock.yaml` only after a successful CLI match. `--skill` is additive across separate installs: a later `apm install <bundle> --skill X` adds `X` to the existing pin (union) rather than replacing it -- previously deployed skills are never silently removed. Use `--skill '*'` to reset to the full bundle; to drop a single skill, edit the `skills:` list in `apm.yml` and re-run `apm install`. |
| `--as ALIAS` | bundle id | Override the log/display label for a local-bundle install. Only valid with a single local-bundle `PACKAGE_REF`. |

#### Skill-filter outcomes

An invalid name passed directly with `--skill` is an
install error. A previously persisted `skills:` selection that no longer
matches an available source skill is a warning instead: install succeeds and
lists the package, declared request names, and available names. Edit `skills:`
in `apm.yml`, then run `apm install` again.

### MCP server entry (use only with `--mcp`)

| Flag | Default | Description |
|---|---|---|
| `--mcp NAME` | unset | Add an MCP server entry to `apm.yml` and install it. Pair with the flags below or pass an executable after `--`. |
| `--transport stdio\|http\|sse\|streamable-http` | inferred | Inferred from `--url` or the post-`--` argv when omitted. |
| `--url URL` | unset | Endpoint for `http`, `sse`, or `streamable-http` transports. Scheme must be `http` or `https`. Codex requires HTTPS for non-loopback endpoints; plain HTTP is accepted only for literal loopback endpoints such as `localhost`, `ip6-localhost`, `127.0.0.0/8`, or `::1`. |
| `--env KEY=VALUE` | unset | Environment variable for stdio MCP servers. Repeatable. |
| `--header KEY=VALUE` | unset | HTTP header for remote MCP servers. Repeatable. Requires `--url`. |
| `--mcp-version VER` | unset | Pin a registry MCP entry to a specific version. |
| `--registry URL` | resolved | Custom MCP registry URL for resolving `--mcp NAME`. Persisted to `apm.yml`. Resolution order: this flag, `MCP_REGISTRY_URL`, `apm config set mcp-registry-url`, then the public default. Not valid with `--url` or a stdio command. |

## Behavior

- **Auto-bootstrap.** `apm install <pkg>` with no `apm.yml` creates a minimal one. Its name comes from the current directory (or home directory for global installs) and falls back to `my-project` if that derived name is invalid. `apm install --dry-run -g <pkg>` validates through a temporary manifest when `~/.apm/apm.yml` is absent, reports the real user manifest path, and leaves `~/.apm` uncreated. If `~/.apm/apm.yml` already exists, global dry-run reads it in place without writing changes. Bare `apm install` with no `apm.yml` exits with a hint to run `apm init` or `apm install <org/repo>`.
- **Target persistence on bootstrap.** When `--target` maps to recognized manifest targets, those target(s) are persisted to the new manifest's `targets:` field so a later bare `apm update` redeploys to the same targets without re-specifying `--target`. For absent user manifests, `apm install --dry-run -g --target ... <pkg>` previews that target field but does not write it.
- **One effective target.** Package primitives, MCP servers, and LSP servers consume one target decision per invocation: `--target` > `apm.yml targets:` > `apm config set target ...` > auto-detect. A saved target therefore applies to `apm install`, `apm install --mcp`, and later `apm update` runs without another flag.
- **Claude LSP discovery.** Project installs write the APM-managed plugin at
  `.claude/skills/apm-lsp/.claude-plugin/plugin.json`; global installs write
  `~/.claude.json`. Existing project-root `.lsp.json` files are preserved for
  manual review because they may contain user-owned entries. See
  [Install LSP servers](../../../consumer/install-lsp-servers/).
- **Required service writes fail loudly.** If MCP or LSP work is declared but no target can be resolved, install exits non-zero before changing the manifest, package deployment, or native service config. A native MCP/LSP config write failure also exits non-zero with the failed target and a permissions/path next step. A successful direct `--mcp` add never reports `Install interrupted`.
- **Direct registry lookup fails closed.** Registry-form MCP entries (`apm install --mcp NAME` with no `--url` and no post-`--` command) resolve one unique registry identity before writing the manifest or user config. An unreachable registry, missing identity, or ambiguous bare server name exits non-zero without changing state.
- **Diff-aware.** Packages whose ref or version changed in `apm.yml` are re-downloaded automatically. MCP servers with matching config are skipped (`already configured`); changed config is re-applied (`updated`).
- **Transactional replacement.** `--update` and `--refresh` download package
  replacements to isolated staging paths and validate them before publication.
  If download, validation, or activation fails, APM keeps the previous package
  and lockfile active and exits non-zero with retry guidance.
- **Git-hook isolation.** Dependency Git operations ignore repository-locating
  hook variables, preserving the caller's branch and HEAD. APM allows safe URL
  rewrites but rejects credentials, insecure transports including `http://` and
  `git://`, remote-helper syntax such as `ext::` and `https::`, and cross-host
  network targets for every host class. Credentials are resolved per
  `(host, port, org)`; private `github.com` helper fallback also uses the
  repository path. Managed and anonymous HTTPS auth is scoped to the effective
  repository URL. See
  [Git URL rewrite safety](../../../getting-started/authentication/#git-url-rewrite-safety).
- **Instruction frontmatter preflight.** Malformed YAML always rejects the
  package before any of its primitives are deployed. Critical hidden characters
  decoded from metadata also prevent installation by default; `--force`
  overrides only that critical finding. Warning-level findings do not prevent
  installation. See [Author primitives](../../../producer/author-primitives/)
  for fence and UTF-8 BOM syntax.
- **MCP-only lock state.** A normal project install creates or updates `apm.lock.yaml` when `apm.yml` declares only MCP dependencies, records the resolved MCP configs and targets, and migrates a legacy `apm.lock` first. Repeating the same install leaves the lockfile and target configs byte-identical. If initial lock creation fails, install exits nonzero and warns with writable-directory and rerun guidance.
- **Lockfile replay and Git ref freshness.** Plain and `--frozen` installs may trust `apm.lock.yaml` and the local Git cache, reusing the locked commit for unchanged Git dependencies across the full resolved graph. In contrast, `apm install --update`, `apm install --refresh`, [`apm update`](../update/) with or without `--force`, [`apm lock --update`](../lock/), and [`apm outdated`](../outdated/) establish mutable Git refs from upstream instead of accepting stale refs from a local bare Git cache. APM picks up upstream changes to a transitive package's `apm.yml` only when you regenerate the graph -- run `apm update` or `apm lock --update`. See the [lockfile specification](../../lockfile-spec/) for the replay contract.
- **Semver ranges on git deps.** `ref:` accepts semver ranges (`^1.2.0`, `~1.4`, `>=2.0 <3`, `1.5.x`) for git-source deps, including positional virtual-subdirectory references. APM runs `git ls-remote` against the dep, picks the highest tag matching the range, and pins the resolved tag plus commit SHA, version, and original constraint in `apm.lock.yaml`. Subsequent installs replay the lockfile without network; use `--update` (or change the manifest constraint) to re-resolve. See [manage dependencies](../../../consumer/manage-dependencies/#pin-a-semver-range) for the supported syntax.
- **No-op nudge.** When the lockfile is already satisfied and nothing needs deploying, install prints `[i] Run 'apm update' to check for newer versions.` so you know the silent success was not a missed refresh.
- **Frozen mode.** With `--frozen`, install resolves only what is in `apm.lock.yaml`. A missing lockfile, a direct dependency missing from it, or MCP config state that differs from `apm.yml` exits `1` before lockfile, target config, deployment, or cache mutation. Cold-cache installs (empty `apm_modules/`) with git `apm_package` deps are tolerated: MCP checks are skipped for absent package directories (the packages will be hydrated by the pipeline), and their MCP server configs are restored from the lockfile so no false drift is reported. Remote `claude_skill` dependencies declared at a repository root or subdirectory are also accepted from their locked type before materialization; once present, the lock type and detected skill shape must agree. Missing local paths still fail. See [`config-consistency`](../../baseline-checks/#config-consistency) for the full manifest rule. Run normal `apm install` to create or repair MCP-only lock state, then retry frozen mode. Add-style invocations (`apm install PACKAGE` and `apm install --mcp NAME`) are rejected because they mutate `apm.yml`. Orphan package lock entries are tolerated; local-path deps are skipped. This is a structural check, not a content check -- run `apm audit --ci` for hash verification.
- **Local `.apm/` deployment.** After dependencies are integrated, primitives in the project's own `.apm/` directory are deployed to the same targets. Local files win on collision. Skipped at `--global` and with `--only mcp`.
- **User-scope root context hint.** Compilation stays explicit. After `apm install -g`, targets with native user-scope instruction files pick up global instructions during install. Targets whose user-scope instruction surface is a root context file require [`apm compile --global`](../compile/#global-compilation); install prints a one-line `[i]` hint and writes no root context file.
- **OpenCode user scope.** `apm install -g --target opencode` deploys skills to
  `~/.config/opencode/skills/`. Run `apm compile -g` to refresh
  `~/.config/opencode/AGENTS.md`, including scoped instruction sections.
- **Project-scope root context hint.** After `apm install`, targets that require [post-install instruction compilation](../../targets-matrix/#post-install-instruction-compilation) print a one-line `[i]` hint when dependency instructions require `apm compile`. The hint names only the root context files that compile will update.
- **Stale-file cleanup.** Files a still-present package previously deployed but no longer produces are removed from the workspace, gated by per-file content hashes recorded in the lockfile (user-edited files are kept with a warning).
- **Interrupted-install recovery.** After a successful install, APM removes inactive resolution backup directories left by interrupted lock-aware runs. Active install backups and entries that do not match APM's staging name format are preserved. Lockless backups from older APM versions are retained because they may belong to a running install; inactive orphaned activity-lock files are reported for manual cleanup. Stop other APM installs, rerun with `--verbose`, then manually delete the reported paths.
- **Enterprise marketplace gate.** When installing from a `*.ghe.com` marketplace, bare cross-repo `repo:` fields (e.g. `repo: owner/repo`) are refused before any network request runs, preventing dependency-confusion attacks. Host-qualify the field to proceed: `repo: corp.ghe.com/owner/repo` for an enterprise dep, or `repo: github.com/owner/repo` for a declared cross-host dep.
- **Security scan.** After target, subset, and executable authorization, APM scans
  exactly the source files it can deploy for hidden Unicode, tag-character, and
  bidi-override patterns. Critical findings block the package and make install
  exit `1`; fix the reported files in the package source and reinstall. Use
  `--force` only after reviewing the findings.
- **Diagnostic summary.** Output is grouped at the end (collisions, replacements, warnings, errors) instead of inline. Use `--verbose` to expand individual file paths.
- **Unresolved hook roots.** A hook command that leaves a supported `${PLUGIN_ROOT}` alias unresolved emits a warning naming the package and a concrete repair. Balance quotes around the complete package-relative path, keep it inside the package, then run `apm install` again. See [Hooks and commands](../../../producer/author-primitives/hooks-and-commands/#hooks) for accepted quoting forms.
- **Declared plugin components.** Every path explicitly listed under a recognized plugin manifest's `agents`, `skills`, `commands`, or `hooks` field must resolve inside that plugin root. A missing or escaping path fails before deployment and lockfile commit; remove the declaration or add the component, then reinstall. Omitted fields and empty lists remain valid.
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
apm install -g microsoft/apm-sample-package --dry-run
```

With `-g`, an absent `~/.apm` remains uncreated while APM previews the user-scope install.

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

:::note[Claude bundles only]
This imperative route deploys Claude plugin bundles (the default `apm pack`
output). A portable Agent Plugins v1 package installs declaratively instead:
declare it in `apm.yml` and run `apm install --target copilot`, which keeps it
whole and registers it without locating or executing Copilot. Stable Copilot
CLI 1.0.81 or newer is required when loading the projection. See
[Install Agent Plugins for Copilot](../../../consumer/copilot-agent-plugins/).
:::

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
| `1` | Install failure: security scan blocked a critical finding, auth error, manifest or required MCP/LSP config write error, dependency resolution error, `--frozen` with a missing lockfile or a direct dependency absent from `apm.lock.yaml`, any reported install error (the diagnostic summary closes with `Installation failed with N error(s)`), or unhandled exception. `--force` does **not** suppress general install errors. The diagnostic summary names the cause. |
| `2` | Usage error: no deployment target detectable (no `--target`, no `target(s):` in `apm.yml`, no default target configured via `apm config set target <value>`, and no harness signal in the project), `--ssh` and `--https` both passed, `--frozen` and `--update` both passed, `--root` combined with `--global`, or a Click flag conflict. |

## Notes

- **`--force` is dual-purpose.** It overwrites locally-authored files on collision **and** disables the critical-finding block from the built-in security scan. It does **not** suppress general install errors -- any error reported in the diagnostic summary still exits `1` (matches `npm` / `pip` / `cargo`). It does **not** refresh remote refs -- for routine ref updates, run [`apm update`](../update/). To remediate a blocked package, fix the reported source files and reinstall; `apm audit --strip` only remediates files that are already deployed. See [Drift and secure by default](../../../consumer/drift-and-secure-by-default/).
- **Target contraction is reconciled.** A narrowed `targets:` in `apm.yml` is reconciled on the next non-dry-run install: deployed files, lockfile ownership, and merge-hook config/sidecar entries for the dropped target are cleaned up, even when no dependency itself changed. A package's own `target:` / `targets:` declaration applies an additional restriction within that effective set. See [Hooks and commands](../../../producer/author-primitives/hooks-and-commands/#hooks) for the full intersection and merge-hook config/sidecar details. `apm lock` may refresh the lockfile rows, but it never deletes deployed files from disk.
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
