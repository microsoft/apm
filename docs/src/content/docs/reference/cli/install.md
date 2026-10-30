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

Repeating an existing positional package without requesting a change reports
`already in apm.yml`, not `updated ref in apm.yml`. It preserves the existing
declaration and does not rewrite an otherwise unchanged manifest. Changed refs
and explicit skill additions or resets still update the declaration.
Explicit transport or resolver changes are not no-ops: an HTTPS request can
replace an HTTP declaration, and a Git-qualified marketplace source can replace
a shorthand routed through the default registry.

`PACKAGE_REF` accepts: shorthand (`owner/repo`), HTTPS or SSH Git URLs, FQDN shorthand (`host/owner/repo`), local paths (`./path`, `/abs/path`, `~/path`), packed bundles (`./bundle.zip`, `./bundle.tar.gz`), and marketplace refs (`NAME@MARKETPLACE[#ref]`).

Local paths may select a directory package or one `.agent.md`,
`.instructions.md`, or `.prompt.md` file, such as
`apm install ./ai/agents/xyz.agent.md`. Single-file installs copy only the selected
file; use a directory package with `apm.yml` for companion assets. Missing paths,
unsupported file kinds, unreadable sources, and invalid primitive content fail
with distinct diagnostics. File symlinks and global project-relative paths are
not admitted.

:::caution
`http://` dependencies are refused unless you pass `--allow-insecure` (direct) or `--allow-insecure-host HOSTNAME` (transitive).
:::

## Options

### Common

| Flag | Default | Description |
|---|---|---|
| `--update` | off | Re-resolve dependencies to the latest version or Git ref allowed by `apm.yml` and rewrite `apm.lock.yaml`. Mutable Git refs must resolve against upstream; APM does not fall back to stale refs from the local bare Git cache. Mutually exclusive with `--frozen`. For interactive use with a confirmation prompt, use [`apm update`](../update/) instead. |
| `--frozen` | off | Require `apm.lock.yaml`, replay its pins, and never rewrite it. Refuse declaration and local-source drift; verify unavailable package MCP evidence after hydration, before deployment. See **Frozen mode** and **Cold frozen restoration** below for validation timing and recorded-file checks. Not a network freshness check. Mutually exclusive with `--update`, positional package additions, and `--mcp`. |
| `--dry-run` | off | [Preview without writing](#preview-without-writing), subject to offline dependency limits. Positional packages and ref changes are validated and previewed without changing an existing `apm.yml`. Project bootstrap keeps its manifest and explicit targets; global bootstrap uses temporary state without creating `~/.apm`. `-g --mcp` writes no user manifest, lockfile, or runtime configuration. |
| `--force` | off | Overwrite locally-authored files on collision **and** bypass the security scan's critical-finding block. Does **not** suppress general install errors (any reported error still exits `1`, matching npm / pip / cargo) or select ref freshness. Add `--update` or `--refresh` to resolve mutable refs upstream; [`apm update`](../update/) does so with or without `--force`. Use only after independent verification. |
| `--verbose`, `-v` | off | Show per-file paths and full error context in the diagnostic summary. |
| `--dev` | off | Add new packages to `devDependencies`. Dev deps install locally but are excluded from `apm pack` output. |

Local-source verification is APM's frozen-install safety behavior, not a new
lockfile format or an OpenAPM conformance requirement. Legacy bare SHA-256
digests remain readable without rewriting the lock. A missing digest requires
a normal install to establish the baseline; cross-implementation local-source
hash portability is not guaranteed.

### Deploy location

| Flag | Default | Description |
|---|---|---|
| `--root DIR` | `$PWD` | Redirect every write -- `apm_modules/`, `apm.lock.yaml`, `.gitignore`, and integrated harness files -- under `DIR`, while `apm.yml`, `.apm/`, and local-path dependencies still resolve from the current working directory. Mirrors `pip install --target` / `npm install --prefix`. `DIR` is created if missing (except under `--dry-run`, which refuses to create it). Not valid with `--global` (user scope), which exits `2`. |

`apm install --root DIR --frozen` checks `DIR/apm.lock.yaml` against the
source manifest. A source-side lockfile is not a substitute for a missing or
stale deployment lock. To create or repair it, rerun the same install command
without `--frozen`, keeping `--root DIR`. User-scope frozen installs likewise
use the user installation's lockfile; retain `--global` when retrying.

### Target selection

| Flag | Default | Description |
|---|---|---|
| `--target`, `-t VALUE` | auto-detect | Force deployment targets. Comma-separated for multiple (`-t claude,cursor`). Values: `copilot`, `claude`, `grok-build`, `cursor`, `opencode`, `codex`, `gemini`, `antigravity`, `windsurf`, `kiro`, `intellij`, `vscode`, `agent-skills`, `hermes`, `grok-bot`, `all`; experimental `copilot-cowork`, `copilot-app`, and `grok-cloud` (skills only) are also accepted when enabled. Hermes and grok-bot are stable but explicit-only. IntelliJ-specific integration is MCP-only and writes JetBrains Copilot's user-scope MCP config; package file primitives use the Copilot profile. `all` excludes `agent-skills`, `antigravity`, `grok-bot`, `hermes`, `intellij`, and all experimental targets; combine them explicitly to add them, for example `all,hermes`. Explicit MCP target lists are exact: `intellij,claude` writes only those two MCP configs. See the precedence note below. With nothing to detect, install exits `2` with a teaching message. |
| `--runtime VALUE` | unset | Legacy alias for `--target` (single value only). Still accepted; prefer `--target`. |
| `--exclude VALUE` | unset | Skip one runtime from the resolved MCP/LSP target set (explicit selection, manifest, saved config, or auto-detection). |
| `--only apm\|mcp` | both | Install only APM packages or MCP/LSP service dependencies. Use `--only=apm` to skip service configuration and `--only=mcp` to select MCP and LSP services only. |
| `-g`, `--global` | off | Install to user scope (`~/.apm/`) instead of the current project. `apm install -g --mcp NAME` creates or updates `~/.apm/apm.yml`, then deploys only to global-capable runtimes, such as Copilot CLI, Claude Code, Cursor, OpenCode, Codex CLI, Gemini CLI, Antigravity CLI, Hermes, Kiro, Windsurf, and JetBrains Copilot. Mixed selections skip workspace-only targets with a warning. A selection with no global-capable target exits `2` before changing the user manifest, lockfile, or runtime configuration. |
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
| `--skill NAME` | all | Install only named skills from a dependency that exposes selectable skills. Applies to both git-longhand and registry-longhand (`id:`/`registry:`) dependencies. Repeatable. For plugin manifests, `NAME` may be the skill name or manifest path, such as `skills/productivity/grill-me`. A CLI name that matches no declared skill is an install error; the diagnostic lists the available names. A non-empty persisted `skills:` selection matching no available skill also fails, preserving the dependency's previous skill files and ownership/hash records. The selection is persisted to `apm.yml` and `apm.lock.yaml` only after a successful CLI match. `--skill` is additive across separate installs: a later `apm install <bundle> --skill X` adds `X` to the existing pin (union) rather than replacing it -- previously deployed skills are never silently removed. Use `--skill '*'` to reset to the full bundle; to drop a single skill, edit the `skills:` list in `apm.yml` and re-run `apm install`. |
| `--as ALIAS` | bundle id | Override the log/display label for a local-bundle install. Only valid with a single local-bundle `PACKAGE_REF`. |

#### Skill-filter outcomes

An invalid name passed directly with `--skill` is an
install error. A non-empty persisted `skills:` selection matching no available
source skill also fails with a nonzero exit, even if all source skills have
disappeared. The error names the dependency, requested skills, and available
skills (or states that none are available). Previous skill files and their
ownership/hash records are preserved, including across repeated failures;
local edits are not accepted as a new integrity baseline.

Correct `skills:` in `apm.yml`, then run `apm install` again. A valid narrower
selection removes unselected managed skills normally; removing the dependency
still cleans up its managed files, subject to the usual user-edit protections.

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

- **Unmanaged skill conflicts fail by default.** A differing user-owned file
  at any selected skill destination refuses the complete selected deployment
  plan before native writes, success-shaped manifest/lock updates, or cleanup.
  This replaces warning-only success that could leave Claude and the shared
  Copilot/Codex root with different skills. The error names the skill,
  dependency, source file, local path, and affected targets. Rename the local
  skill, or edit an existing `skills:`/`targets:` selection to exclude the
  conflict. A skill allow-list does not automatically include future skills.
  Matching names or shared directories alone are not conflicts; identical
  files and exact managed paths retain their existing handling. Preparation
  may populate caches; refusal does not delete an older partial installation
  or promise rollback for later races, scripts, or unrelated failures.
  `--force` remains explicitly destructive and also bypasses the critical
  security-scan block; it is not the routine remedy.

- **Stale-file cleanup.** `Cleaned N stale deployed files for DEPENDENCY` names
  the affected dependency, not a deletion location. Cleanup removes deployed
  files; a local dependency's source directory is not cleaned.

- **Historical missing-file claims.** Normal `apm install` retires an obsolete
  dependency file claim and its hash when the file is already absent and
  complete source discovery and successful skill deployment prove it is no
  longer shipped within a current source-backed skill. This also repairs older
  locks with `legacy` target records or no deployment ledger, without a lock
  reset or uninstall/reinstall.
  Other owners, unprocessed targets and incomplete deployments remain protected.
  Missing bytes, a saved hash or a directory prefix alone are not proof;
  unresolved claims still appear in audit. Still-present content with unclear
  ownership is not deleted or adopted by this repair. Audit is read-only;
  `--frozen` neither repairs these claims nor accepts changed local sources.
  After ordinary repair, unchanged frozen replay remains supported.

- **Immutable dependency conflicts.** Install fails when two paths require
  different immutable commits for one package identity, showing both paths and
  requested refs as ordered `owner/repo@ref -> owner/repo@ref` chains.
  Equivalent tag/SHA spellings remain valid. Unchanged locked refs reuse their
  recorded commits without ref discovery. `--frozen` also
  checks transitive immutable requirements against locked commits as package
  manifests become available; a cold cache may need parent-package downloads
  before a conflict is discoverable. Short SHA pins must match the recorded
  full commit's prefix. Align the root/parent refs and run a normal
  install to regenerate the lockfile. See
  [Manage dependencies](../../../consumer/manage-dependencies/#incompatible-immutable-requirements).

- **Auto-bootstrap.** `apm install <pkg>` with no `apm.yml` creates a minimal one. Its name comes from the current directory (or home directory for global installs) and falls back to `my-project` if that derived name is invalid. `apm install --dry-run -g <pkg>` validates through a temporary manifest when `~/.apm/apm.yml` is absent, reports the real user manifest path, and leaves `~/.apm` uncreated. If `~/.apm/apm.yml` already exists, global dry-run reads it in place without writing changes. Bare `apm install` with no `apm.yml` exits with a hint to run `apm init` or `apm install <org/repo>`.
- **Target persistence on bootstrap.** When `--target` maps to recognized manifest targets, those target(s) are persisted to the new manifest's `targets:` field so a later bare `apm update` redeploys to the same targets without re-specifying `--target`. For absent user manifests, `apm install --dry-run -g --target ... <pkg>` previews that target field but does not write it.
- **Package target narrowing.** A package `targets:` list narrows the active targets. Packages declaring `agent-skills` are also accepted by the `grok-bot` target. With an explicit `--target`, the install fails only when no dependency has a compatible target, or when a package named on the command line and its whole subtree have none; other filtered dependencies warn. For targets from `apm.yml`, config, or auto-detect, filtering only warns.
- **Grok Bot installs.** For a live Grok Bot, use `apm install --target grok-bot --global`. Grok Bot reads skills from `~/agent-data/workflows/`; a project-scope install only lands in `./agent-data/workflows/` under the current folder.
- **One effective target.** Package primitives, MCP servers, and LSP servers consume one target decision per invocation: `--target` > `apm.yml targets:` > `apm config set target ...` > auto-detect. A saved target therefore applies to `apm install`, `apm install --mcp`, and later `apm update` runs without another flag.
- **Claude LSP discovery.** Project installs write the APM-managed plugin at
  `.claude/skills/apm-lsp/.claude-plugin/plugin.json`; global installs write
  `~/.claude.json`. Existing project-root `.lsp.json` files are preserved for
  manual review because they may contain user-owned entries. See
  [Install LSP servers](../../../consumer/install-lsp-servers/).
- **Required service writes fail loudly.** If MCP or LSP work is declared but no target can be resolved, install exits non-zero before changing the manifest, package deployment, or native service config. A native MCP/LSP config write failure also exits non-zero with the failed target and a permissions/path next step. A successful direct `--mcp` add never reports `Install interrupted`.
- **Direct registry lookup fails closed.** Registry-form MCP entries (`apm install --mcp NAME` with no `--url` and no post-`--` command) resolve one unique registry identity before writing the manifest or user config. An unreachable registry, missing identity, or ambiguous bare server name exits non-zero without changing state.
- **Diff-aware.** Packages whose ref or version changed in `apm.yml` are re-downloaded automatically. MCP servers with matching config are skipped; unchanged APM-managed connections can be updated when their declaration changes.
- **Claude/Codex MCP collisions.** A differing self-defined connection with the same name is preserved unless its native configuration still matches the recorded APM baseline. Install identifies the conflicting target and exits nonzero; successful writes to other targets remain recorded. An identical foreign entry is not adopted. Neither `--force` nor `--frozen` takes over a user connection. Removal also preserves entries with a differing or unverifiable baseline. See [MCP conflict recovery](../../../consumer/install-mcp-servers/#resolve-a-connection-name-conflict).
- **Claude Code profiles.** For user-scope MCP installs, an absolute `CLAUDE_CONFIG_DIR` selects that profile's `.claude.json` for both installation and stale-server cleanup. Removing an APM-managed server in the selected profile preserves other profiles and user-owned entries. Cleanup refuses symlinked config files or profile directories; replace the link with a regular file or directory before retrying. Skills, agents, and other deployed files follow the same rule: profiles share one user lockfile, and a global install removes stale files only under the selected `CLAUDE_CONFIG_DIR` (or `~/.claude`), leaving files deployed to other profiles in place. Files of a profile you no longer install into stay on disk and in the lockfile; remove them manually.
- **Transactional replacement.** `--update` and `--refresh` download package
  replacements to isolated staging paths and validate them before publication.
  Failed downloads or validation leave the previous package active. Failed
  activation attempts restoration and exits non-zero without changing the
  lockfile. If the OS also denies restoration, APM reports both failures and
  retains recovery data; the prior package may no longer be active.
  Follow [Windows recovery guidance](../../../troubleshooting/install-failures/#windows-file-locks-and-incomplete-recovery)
  before retrying.
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
- **Lockfile replay and Git ref freshness.** Plain and `--frozen` installs reuse the locked commit for matching Git dependencies, declared refs and host providers across the resolved graph, without checking whether upstream branches moved. A mutable ref without a matching lock entry resolves upstream instead of reusing a stale bare-cache ref, even if another package or sibling path is locked. In contrast, `apm install --update`, `apm install --refresh`, [`apm update`](../update/) with or without `--force`, [`apm lock --update`](../lock/), and [`apm outdated`](../outdated/) establish mutable Git refs from upstream. APM picks up upstream changes to a transitive package's `apm.yml` only when you regenerate the graph -- run `apm update` or `apm lock --update`. See the [lockfile specification](../../lockfile-spec/) for the replay contract.
- **Semver ranges on git deps.** `ref:` accepts semver ranges (`^1.2.0`, `~1.4`, `>=2.0 <3`, `1.5.x`) for git-source deps, including positional virtual-subdirectory references. APM runs `git ls-remote` against the dep, picks the highest tag matching the range, and pins the resolved tag plus commit SHA, version, and original constraint in `apm.lock.yaml`. Subsequent installs replay the lockfile without network; use `--update` (or change the manifest constraint) to re-resolve. See [manage dependencies](../../../consumer/manage-dependencies/#pin-a-semver-range) for the supported syntax.
- **No-op nudge.** When the lockfile is already satisfied and nothing needs deploying, install prints `[i] Run 'apm update' to check for newer versions.` so you know the silent success was not a missed refresh.
- **Conflicted lockfiles.** A lockfile containing unresolved Git merge conflict markers exits `1` for required readers and names the file; the conflicted bytes are never rewritten. Preview retains its warning and exit `0`. See [manual conflict repair](../../../troubleshooting/install-failures/#merge-conflict-markers-in-the-lockfile).
- **Dependency versions after checkout.** Manifest-derived versions describe the acquired package, not an older copy in `apm_modules/`. The first ordinary install after switching branches records the current version; a second install is not needed to correct it. These values are inventory metadata, not frozen replay pins. Registry and Git-semver versions retain their resolver-selected meaning, which can differ from the package manifest or tag spelling.
- **Skill bytes after checkout.** APM records successful rendered skill writes
  locally, separately from the lockfile. After a branch switch, an unchanged
  prior deployment can update even if modules and cache were removed. Edited
  files remain protected, including edits matching an older package version.
  The ignored `.apm-local/` installation identity and
  `~/.apm/deployment-receipts/` are local state, not files to commit or share.
  The identity lives in the project root, or your home directory for `--global`.
  Configured external skill roots use the same ownership and receipt checks.
  Switching targets that share a skill path refreshes valid same-writer evidence
  through a guarded copy, even when the source bytes are unchanged.
  Missing evidence, including installations predating these receipts, does not
  authorize overwriting existing files. Preserve your edits and move the
  conflicting file aside before reinstalling. Invalid evidence fails explicitly.
  Creating this state requires filesystem hard-link support. State-write failures
  are reported; successful earlier deployment writes are not rolled back.
  Audit and dry-run never establish this authority. Frozen install may record
  successful local writes but still preserves manifest and lockfile bytes.
- **Frozen mode.** With `--frozen`, install replays the locked graph and **never writes `apm.lock.yaml`**. Missing lockfiles or direct dependencies, changed refs, full commit pins, host providers, HTTP/HTTPS transport, and available MCP declaration mismatches fail before the pipeline. This includes transport changes under an unchanged semver range. Add-style invocations (`apm install PACKAGE` and `apm install --mcp NAME`) are rejected because they mutate `apm.yml`. Orphan package lock entries do not fail manifest matching. Local-content verification ignores unused local entries, but MCP preflight can still reject unavailable local sources. Selected direct and transitive local-path dependencies are checked against their locked content hash before lifecycle or deployment: the hash covers the validated local-copy image, including dereferenced in-package symlinks, before integration transforms it. A missing entry or legacy lock without that hash requires a normal `apm install` to establish a baseline. See [`config-consistency`](../../baseline-checks/#config-consistency) for the full manifest rule. Use normal install to establish or intentionally repair lock state; use `apm audit --ci` for on-disk content verification.
- **Cold frozen restoration.** A fresh checkout using a package-installing mode can restore supported locked remote packages, including whole plugins, without an ordinary-install-first workaround. Missing package bytes are not proof that their MCP declarations were removed. When the lock identifies an unavailable source unambiguously, APM downloads its pinned content and checks the actual restored declarations before deployment or native MCP configuration changes. Changed or removed declarations, ambiguous/shared origin evidence, invalid materialized shapes, missing local sources, and insufficient lock evidence still fail. A deferred MCP verification failure preserves previous native MCP configuration and user-owned settings; downloads/cache fills may already have happened. This is not rollback of arbitrary lifecycle scripts or every frozen failure.
  Inactive package lock entries without MCP declarations do not require hydration. This does not exempt active packages whose source evidence is still unavailable, or skip newly added declarations when the locked MCP set is empty.
  `--only=mcp` does not hydrate package sources, so it refuses unavailable MCP evidence rather than deferring it. Use the full `apm install --frozen` command to restore those sources. Root MCP precedence, transitive trust and native-plugin MCP ownership remain unchanged. Frozen replay does not refresh a discovery catalog or follow its latest selection. The lockfile does not contain package bytes: an uncached runner still needs access and any required credentials for the pinned source.
- **Frozen mode fails rather than rewriting.** If the install deploys files the committed `apm.lock.yaml` does not record, `--frozen` exits `1` and names those paths instead of silently updating the lockfile. A file no lockfile row claims carries no recorded hash, which leaves it outside `content-integrity`'s hash comparison *and* its hidden-Unicode scan, so this is the install-time half of that guarantee; `apm audit --ci` remains the on-disk content check. Unlike the structural checks above this one runs after the pipeline, so the deployed files are already on disk -- only the lockfile write was withheld. Recover by running `apm install` without `--frozen` and committing the updated `apm.lock.yaml`. The check is one-directional and matches the tolerance above: claims the install would *drop* (a `--target` filter, `--only`, a removed dependency) do not fail. `generated_at` and `apm_version` are excluded, so a newer CLI reading an older lockfile is not a rewrite.
- **Frozen native-service targets.** The no-write boundary includes late MCP/LSP integration and MCP-only installs: `apm.yml` and `apm.lock.yaml` keep their exact bytes, including legacy timestamps. Frozen mode replays recorded target/server ownership, not an unrecorded personal deployment. If Copilot and Claude own a server but IntelliJ does not, `apm install --frozen --target copilot,claude,intellij` exits `1`, names the missing target/server claim, and leaves native MCP/LSP configs and user entries unchanged. Run the same command without `--frozen`, keeping the intended `--target`, `--only`, `--root DIR` or `--global` options, to record that ownership; commit the lockfile, then replay frozen. `--only mcp` does not hydrate package sources, even without `--frozen`. Restore the package sources and declarations matching the lock using `apm install` without `--only mcp` in the same deployment store (retain `--root DIR` or `--global`, if used) before retrying. Reordered, repeated and narrower selections remain valid. Source acquisition may precede this check; this is not a general rollback guarantee for package deployment or lifecycle scripts.
  Late MCP checks use the complete resolved package graph, not just the packages selected for installation.
- **Local `.apm/` deployment.** After dependencies are integrated, primitives in the project's own `.apm/` directory are deployed to the same targets. Local files win on collision. Skipped with `--only mcp`. At `--global` the source root is `$HOME`, so `~/.apm/` primitives deploy to user-scope targets.
- **User-scope skill paths.** Global skill installation reads the lockfile from the canonical user metadata root (`~/.apm/`), separate from the skill deployment root. `HOME` or `APM_HOME` directory symlink aliases work without extra flags; package-descendant symlinks and symlinked skill destinations remain rejected. Explicit dependency `alias:` placement is separate; unrelated user files remain protected.
- **Shared skill assets.** When dependencies deploy the same skill directory,
  matching managed files still use last-writer behavior, but reinstalling one
  package preserves the other's unique assets. This applies to project and
  global installs, including shared `.agents/skills/` destinations. Ownership
  and hashes record successfully deployed files, not unrelated destination files.
  Overwrite warnings name the previous owner when known and suggest removing
  one package to avoid the overlap.
  Edited or untracked files remain protected; `--force` permits overwriting
  matching files, not deleting an entire shared directory. Ordinary install
  reconciles changed sources and ownership; `--frozen` keeps the recorded owner
  of overlapping files instead of restoring a relinquished claim.
  An unreadable or missing expected source directory fails installation rather
  than treating an incomplete scan as a successful replacement.
- **User-scope root context hint.** Compilation stays explicit. After `apm install -g`, targets with native user-scope instruction files pick up global instructions during install. Targets whose user-scope instruction surface is a root context file require [`apm compile --global`](../compile/#global-compilation); install prints a one-line `[i]` hint and writes no root context file.
- **OpenCode user scope.** `apm install -g --target opencode` deploys skills to
  `~/.config/opencode/skills/`. Run `apm compile -g` to refresh
  `~/.config/opencode/AGENTS.md`, including scoped instruction sections.
- **Project-scope root context hint.** After `apm install`, targets that require [post-install instruction compilation](../../targets-matrix/#post-install-instruction-compilation) print a one-line `[i]` hint when dependency instructions require `apm compile`. The hint names only the root context files that compile will update.
- **Stale-file cleanup.** Files a still-present package previously deployed but no longer produces are removed only when recorded hashes match (user-edited files are kept with a warning). The same checks apply to project-local `.apm/` output with recorded concrete target ownership, including after renames, deletions, or target removal; [ambiguous legacy rows](../../lockfile-spec/#unattributed-rows) remain untouched. Shared `.agents/skills/` files stay while another package or target claims them, including a declared target skipped by `--target`.
  Narrowing one dependency's `targets:` also reconciles its removed targets
  within the current install scope, even when the consumer still enables those
  targets. Successfully removed files lose their file/hash/ledger claims;
  edited files and targets skipped by `--target` retain protective records.
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

`--dry-run` reuses the install resolver read-only: local source metadata and
unchanged installed remote metadata supply required indirect APM dependencies,
which are included in preview counts. Missing or unreadable metadata, changed
remote refs, `--update` or `--refresh` requiring fetching, failed `--frozen`
transitive pin checks, or unresolved marketplace lookups
mark the dependency preview incomplete and suppress orphan-removal predictions.
An incomplete summary reports the locally known count, not a complete install plan.
This graph pass performs no fetching, materialization, or writes; positional
validation may still access the network. Stale-file integration cleanup is not
previewed.

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
| `0` | Successful install or `--dry-run` preview. A preview does not certify real install success. For Agent Plugins v1 packages, a mixed install still succeeds when target exclusion skips one package but at least one other package deploys. |
| `1` | Install failure: security scan blocked a critical finding, auth error, manifest or required MCP/LSP config write error, dependency resolution error, Agent Plugins v1 target exclusion left no package deployed on a non-dry-run install, `--frozen` with a missing lockfile, a direct dependency absent from `apm.lock.yaml`, or an install that deploys files `apm.lock.yaml` does not record, any reported install error (the diagnostic summary closes with `Installation failed with N error(s)`), or unhandled exception. `--force` does **not** suppress general install errors. The diagnostic summary names the cause. |
| `2` | Usage error: no deployment target detectable (no `--target`, no `target(s):` in `apm.yml`, no default target configured via `apm config set target <value>`, and no harness signal in the project), `--ssh` and `--https` both passed, `--frozen` and `--update` both passed, `--root` combined with `--global`, or a Click flag conflict. |

## Notes

- **`--force` is dual-purpose.** It overwrites locally-authored files on collision **and** disables the critical-finding block from the built-in security scan. It does **not** suppress general install errors -- any error reported in the diagnostic summary still exits `1` (matches `npm` / `pip` / `cargo`). It does **not** refresh remote refs -- for routine ref updates, run [`apm update`](../update/). To remediate a blocked package, fix the reported source files and reinstall; `apm audit --strip` only remediates files that are already deployed. See [Drift and secure by default](../../../consumer/drift-and-secure-by-default/).
- **Agent Plugin target exclusion is fail-loud on non-dry-run total no-ops.** Agent Plugins v1 packages register natively only with the effective `copilot` target today. If every selected target excludes native registration and no other package deploys, a non-dry-run install exits `1` and prints a skill-subpath recovery command such as `apm install kunchenguid/lavish-axi/skills/lavish#main --target codex`. Keep refs after the skill path to install a plain skill bundle, or select `--target copilot` for native registration. Mixed installs that deploy at least one other package still exit `0`. For this target exclusion, `--dry-run` remains a successful preview without the per-package recovery diagnostic; it does not certify that a real install will succeed.
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
- [CLI upgrades](../../../consumer/update-and-refresh/#update-the-apm-cli-binary) -- use your package manager, or `apm self-update` for standalone installs.
- [`apm prune`](../prune/) -- remove orphaned packages and stale files.
- [Registries](../../../guides/registries/) -- end-to-end guide for registry-sourced dependencies.
- [`apm audit`](../audit/) -- explicit security reporting and remediation after install.
- [`apm targets`](../targets/) -- print which harnesses APM detects in the current directory.
- [Install packages (consumer guide)](../../../consumer/install-packages/) -- task-oriented walkthrough.
- [Manifest schema](../../manifest-schema/) -- field reference for `apm.yml`.
- [Lockfile spec](../../lockfile-spec/) -- field reference for `apm.lock.yaml`.
