# CLI Command Reference

## Project setup

| Command | Purpose | Key flags |
|---------|---------|-----------|
| `apm init [NAME]` | Initialize a new APM project | `-y` skip prompts, `--plugin` plugin authoring mode, `--marketplace` seed apm.yml with a `marketplace:` block. After init, Next Steps contextually suggests `agentrc init` (if agentrc is in PATH) or prints a tip link when no agent instruction files exist. |

## Dependency management

| Command | Purpose | Key flags |
|---------|---------|-----------|
| `apm install [PKGS...]` | Install APM and MCP dependencies (supports APM packages, Claude skills (SKILL.md), and plugin collections (plugin.json)) | `--update` (deprecated; prefer `apm update`) refresh refs, `--refresh` re-fetch all deps from upstream and re-resolve all ref pins, `--force` overwrite (does NOT refresh refs; use `apm update` for that), `--frozen` CI-safe install that fails fast when `apm.lock.yaml` is missing or out of sync with `apm.yml` (mutually exclusive with `--update`; structural presence check only -- use `apm audit` for SHA integrity), `--dry-run`, `--verbose`, `--only [apm\|mcp]`, `--target` (comma-separated, e.g. `--target claude,cursor`; highest-priority entry in the resolution chain `--target` > apm.yml `targets:` > auto-detect; on auto-bootstrap when no `apm.yml` exists, recognized manifest target(s) are persisted to the new manifest's `targets:` so a later bare `apm update` reuses them; `--target all` deprecated, see `apm compile --all`; use `kiro` for Kiro IDE; use `copilot-cowork` with `--global` after `apm experimental enable copilot-cowork`; use `hermes` after `apm experimental enable hermes` to deploy skills + `AGENTS.md` and, at `--global`, MCP servers to `~/.hermes/config.yaml`), `--dev`, `-g` global (MCP deploys only to user-scope runtimes: Copilot CLI, Claude Code, Codex CLI, Gemini CLI, Antigravity CLI, Kiro, Windsurf, JetBrains Copilot, and Hermes when enabled), `--trust-transitive-mcp`, `--parallel-downloads N`, `--allow-insecure`, `--allow-insecure-host HOSTNAME`, `--skill NAME` install named skill(s) from a skill collection (SKILL_BUNDLE or plugin manifest; repeatable; plugin manifests accept a leaf name or manifest path; persisted in apm.yml; additive across separate installs -- a later `--skill X` adds to the existing pin (union) rather than replacing it, so previously deployed skills are never silently removed; `'*'` resets to the full bundle; drop a single skill by editing the `skills:` list in apm.yml then re-running install), `--legacy-skill-paths` restore per-client skill dirs, `--mcp NAME` add MCP entry (NAME goes through the same `--target` > `targets:` > auto-detect resolver as APM packages, so a project whitelisting `targets: [copilot]` will not write `.cursor/mcp.json` even if `.cursor/` exists; `apm install -g --mcp NAME` writes user-scope and bypasses the project-scope gate by design), `--transport`, `--url`, `--env KEY=VAL`, `--header KEY=VAL`, `--mcp-version`, `--registry URL` custom MCP registry, `--root DIR` redirect writes (`apm_modules/`, lockfile, `.gitignore`, integrated harness files) under DIR while `apm.yml`/`.apm/`/local deps resolve from `$PWD` (mirrors `pip install --target`; created if missing; not valid with `-g`/`--global`, which exits 2) |
| `apm targets` | Show resolved deployment targets for the current project (Click group; reads filesystem signals; works with or without `apm.yml`) | `--all` also include the `agent-skills` meta-target (only meaningful with `--json`), `--json` machine-readable output. No provenance line is printed (the table is the provenance). |
| `apm uninstall PKGS...` | Remove packages (accepts `owner/repo` or `name@marketplace`) | `--dry-run`, `-g` global |
| `apm prune` | Remove orphaned packages | `--dry-run` |
| `apm deps list` | List installed packages | `-g` global, `--all` both scopes, `--insecure` |
| `apm deps tree` | Show dependency tree | -- |
| `apm deps why PKG` | Explain why a package is installed (walks lockfile bottom-up to direct deps; analogue of `npm why` / `yarn why`) | `-g` global, `--json` |
| `apm find <PATH>` | Trace a deployed file back to the package(s) that contributed it (inverse of install; reads `apm.lock.yaml` only) | `--source` show OCI/git/local origin, `--path` show full why-chain (same as `apm deps why`) |
| `apm view PKG [FIELD]` | View package details, git refs, or registry versions | `-g` global, `FIELD=versions`, `--registry [NAME]` forces registry path for versions |
| `apm outdated` | Check locked deps via SHA/semver comparison; patterned per-package tags are auto-detected; full-SHA pins compare against the latest annotated semver tag | `-g` global, `-v` verbose, `-j N` parallel checks |
| `apm deps info PKG` | Alias for `apm view PKG` local metadata | -- |
| `apm deps clean` | Clean dependency cache | `--dry-run`, `-y` skip confirm |
| `apm deps update [PKGS...]` | Deprecated -- use `apm update` instead (now a strict superset). Update specific packages | `--verbose`, `--force`, `--target` (comma-separated), `--parallel-downloads N`, `-g/--global`, `--legacy-skill-paths` |

### Install validation chain (virtual subdirectory packages)

`apm install` validates subdirectory packages (`owner/repo/path#ref`) before writing to `apm.yml` using the same credential chain as the actual install. See [Authentication > Install validation chain](../authentication/) for the full probe sequence and troubleshooting.

When a default registry is configured, plain shorthand deps (`owner/repo#<ref>`) bypass the GitHub probe. `apm install` requires a version selector before writing to `apm.yml`; deps with no `#<ref>` at all are rejected. Semver selectors (`1.0.0`, `^1.2.3`) use range matching; non-semver selectors (`stable`, `v1.4.2`, any opaque label) are matched exactly against the registry's published versions.

### Target resolution chain

`apm install` resolves harness targets in strict priority order:

1. `--target` flag (highest; CSV form: `--target claude,cursor`).
2. `apm.yml` `targets:` list (or singular `target:` sugar).
3. `apm config set target <value>` default.
4. Auto-detect from filesystem signals (`.claude/` or `CLAUDE.md` -> claude, `.cursor/` -> cursor, `.github/copilot-instructions.md` or any of `.github/instructions/`, `.github/agents/`, `.github/prompts/`, `.github/hooks/` -> copilot, `.codex/` -> codex, `.gemini/` or `GEMINI.md` -> gemini, `.opencode/` -> opencode, `.windsurf/` -> windsurf, `.kiro/` -> kiro, the user-scope JetBrains Copilot MCP config directory `github-copilot/intellij/` -- `%LOCALAPPDATA%\github-copilot\intellij\` on Windows, `~/Library/Application Support/github-copilot/intellij/` on macOS, `~/.local/share/github-copilot/intellij/` on Linux -> intellij). All signals except JetBrains are project-scoped repo markers; the JetBrains signal is a machine-global user-scope directory, so once the Copilot plugin is installed it is detected for every project on that machine.

`apm install` prints a one-line provenance summary before any mutation:

```
[i] Targets: claude, copilot  (source: auto-detect from CLAUDE.md, .github/copilot-instructions.md)
```

Suppress with `--quiet`. Add `--verbose` to also print a `[>] Scanned: ...` line listing every signal probed.

If no `--target`, no `targets:` in `apm.yml`, and no harness signal is present, `apm install` exits 2 with a teaching message instead of silently defaulting to copilot. Run `apm targets` to inspect what APM detects in the current directory; use it for discovery, scripting (`--json`), and debugging unexpected detection.

`apm compile` continues to use legacy auto-detection with a `vscode`/`minimal` fallback for unsignalled projects -- bringing it onto the strict resolution chain is tracked as a follow-up.

## Compilation

| Command | Purpose | Key flags |
|---------|---------|-----------|
| `apm compile` | Compile agent context | `-o` output, `-t` target (comma-separated; resolution chain `--target` > apm.yml `targets:` > auto-detect), `--all` compile for every canonical target (preferred over deprecated `--target all`), `-g`/`--global` (read global instructions from `~/.apm/apm_modules/`, write user-scope root files; cannot combine with project-output flags such as `--target`, `--all`, `--watch`, `--root`, or `--output`; critical hidden-character findings stop the write and exit 1), `--chatmode`, `--dry-run`, `--no-links`, `--watch`, `--validate`, `--single-agents`, `-v` verbose, `--local-only`, `--clean`, `--with-constitution/--no-constitution`, `--force-instructions` / `--no-dedup` (opt out of Claude/Copilot deduplication), `--root DIR` redirect generated artifacts under DIR while sources resolve from `$PWD` (mirrors `pip install --target`; not valid with `--watch`) |

`apm compile --watch` live-reloads `apm.yml`: editing `target:` / `targets:` mid-session takes effect on the next file event without restarting the watcher. The CLI `--target` flag, when passed to `apm compile --watch`, still outranks `apm.yml`. Re-resolution is gated on the changed file's basename being `apm.yml`, so `.instructions.md` edits do not pay an extra resolver round-trip and a stray `backup_apm.yml` cannot trigger a reload. `--clean` is ignored in watch mode and the watcher prints an explicit `[!]` warning at startup (`--clean is ignored in watch mode; run 'apm compile --clean' separately to remove orphaned outputs.`); run `apm compile --clean` separately between watch sessions to remove orphans.

When `apm install` has already deployed instructions to `.claude/rules/`, `apm compile --target claude` omits the Project Standards section from `CLAUDE.md` to avoid Claude Code seeing every instruction twice. Detection is a simple glob (`.claude/rules/*.md`). `CLAUDE.md` is still generated when it carries a constitution block or dependency `@import` paths -- only the instructions section is suppressed. An informational log message is emitted when zero `CLAUDE.md` files are generated because all content was already deployed via rules.

**Deduplication override.** Pass `--force-instructions` (alias: `--no-dedup`) to always include the instructions section in `CLAUDE.md`, regardless of `.claude/rules/` contents.

**Stale `CLAUDE.md` removal (`--clean`).** When `.claude/rules/` is populated and an APM-generated `CLAUDE.md` (identified by the `<!-- Generated by APM CLI -->` marker) is present, `apm compile --target claude --clean` removes that stale file if no constitution or dependency import keeps `CLAUDE.md` active; otherwise it regenerates the file. Hand-authored `CLAUDE.md` files (no marker) are never deleted; a warning is emitted instead so you know the duplicate context remains.

**Dry-run preview.** Use `--dry-run` with `--clean` to preview what would be removed without touching any files. If a hand-authored `CLAUDE.md` would block deletion, the preview surfaces that outcome as well.

When `apm install --target copilot` has already deployed instructions to `.github/instructions/`, `apm compile --target copilot` omits `AGENTS.md` entirely if its only content would be the duplicated instructions section. `AGENTS.md` is still generated when it carries non-instruction content such as a constitution. Pass `--force-instructions` (alias: `--no-dedup`) to force full `AGENTS.md` output.

## Scripts

| Command | Purpose | Key flags |
|---------|---------|-----------|
| `apm run SCRIPT` | Execute a named script | `-p name=value` (repeatable) |
| `apm preview SCRIPT` | Preview script without running | `-p name=value` |
| `apm list` | List available scripts | -- |

## Security and audit

| Command | Purpose | Key flags |
|---------|---------|-----------|
| `apm audit [PKG]` | Scan installed primitives for hidden Unicode, drift, and lockfile/policy violations | `--file PATH`, `--strip`, `--dry-run`, `-v`, `-f [text\|json\|sarif\|md]`, `-o PATH`, `--ci`, `--policy SOURCE`, `--no-cache`, `--no-fail-fast`, `--no-drift`, `--external NAME` (experimental; ingest a third-party SARIF scanner, e.g. `skillspector`), `--external-sarif PATH`, `--external-llm/--no-external-llm`, `--external-args TEXT` |

`apm audit` runs **drift detection by default** (issue #1071). It replays `apm install` cache-only into a temporary scratch tree and diffs the result against your working tree. Catches three failure modes: (1) `.apm/` source added without re-running `apm install`, (2) hand-edits to deployed files that diverge from canonical source, (3) orphan files left after their source was removed. The scan is read-only -- never writes to your project, lockfile, or `apm_modules/`. Build IDs, CRLF line endings, and BOMs are normalized away so they cannot trigger false positives. If the install cache has not been warmed (e.g. a fresh checkout before the first `apm install`), the drift check is skipped with an informational message rather than failing; run `apm install` to warm the cache and enable the check on the next run. Use `--no-drift` to opt out (e.g. fast inner loops); the flag is mutually exclusive with `--strip`/`--file`. In `--ci` mode drift findings produce exit code 1 alongside the seven baseline lockfile checks. Drift output is integrated into JSON (top-level `drift` key) and SARIF (rule IDs `apm/drift/<kind>` where kind is `modified`/`unintegrated`/`orphaned`).

**External scanners (experimental, behind `apm experimental enable external-scanners`).** `--external NAME` runs a third-party SARIF scanner (e.g. `skillspector`) and merges its findings. `--external-llm/--no-external-llm` toggles LLM-powered analysis (default off; sends scanned content to a third-party API, so APM prints a `[!]` egress banner and forwards `OPENAI_API_KEY`/`NVIDIA_INFERENCE_KEY` only when on). `--external-args TEXT` is a single shlex-split string of extra scanner flags, validated against a per-adapter allowlist -- non-allowlisted flags, secret-looking flags, and out-of-cwd paths are rejected fail-closed. `--external-llm`/`--external-args` without `--external` is a usage error (exit 2). Scanner configuration or infrastructure errors (feature disabled, scanner not found, malformed SARIF) exit **3**. Persist defaults with `apm config set external.<name>.llm true` and `apm config set external.<name>.args -- "--model gpt-4o"`. Precedence: CLI > config > policy floor.

## Lifecycle scripts

| Command | Purpose | Key flags |
|---------|---------|-----------|
| `apm lifecycle` | List all discovered lifecycle scripts across policy, user, and project sources | -- |
| `apm lifecycle init` | Inject a starter `lifecycle:` block into `apm.yml` | `--force` (overwrite existing block) |
| `apm lifecycle test EVENT` | Fire a synthetic event through all discovered scripts (dry-run) | `--verbose`, `--execute` (actually run scripts) |
| `apm lifecycle validate` | Check all discovered script files for schema errors, unknown events, missing fields, and non-HTTPS URLs | -- |
| `apm lifecycle trust` | Trust `apm.yml` `lifecycle:` at its current contents so project scripts run on install | -- |
| `apm lifecycle untrust` | Revoke trust for `apm.yml` `lifecycle:`; project scripts will stop running | -- |

Lifecycle scripts fire on six events: `pre-install`, `post-install`, `pre-update`, `post-update`, `pre-uninstall`, `post-uninstall`. Script files are discovered from three sources (additive): policy (`/etc/apm/policy.d/*.json`, JSON), user (`~/.apm/apm.yml`, YAML), project (`apm.yml` `lifecycle:` at repo root, YAML). Two script types: `command` (shell via subprocess, event JSON on stdin) and `http` (HTTPS POST). Script output is appended to `~/.apm/logs/scripts.log`. See the [Lifecycle scripts](/apm/enterprise/lifecycle-scripts/) guide for full documentation.

## Distribution

| Command | Purpose | Key flags |
|---------|---------|-----------|
| `apm pack` | Build distributable artifacts (bundle and/or marketplace.json -- driven by `apm.yml`). Default output is a Claude Code plugin directory. Bundles are **target-agnostic**: `pack.target` is recorded in every bundle for diagnostic purposes (typically `"all"` for target-agnostic packs, or the project's detected target) and is not authoritative at install time; `pack.bundle_files` (path -> sha256) drives integrity verification. The consumer's project decides where files land. Dependency content is packed **exclusively** from lockfile-attested `deployed_files` (in both `--format plugin` and the default `--format apm`); the `apm_modules` cache is never packed. Each file is verified against its `deployed_file_hashes` SHA-256 before inclusion, so a file tampered after `apm install` (hash mismatch) or deleted (missing on disk) fails the pack with a message pointing at `apm install`; files with no recorded hash (older lockfiles) pack unverified. Dependency hooks-config / MCP-config is not attested, so it is not packed -- `apm pack` warns (`[!]`) and names the dependency (first-party root hooks/MCP are still packed). Marketplace-publishing projects (`marketplace:` block, no `dependencies:`) no longer emit the misleading "No plugin.json found" warning; after a successful build, a vendor-neutral catalog of artifact paths is appended together with a single docs pointer (`producer/publish-to-a-marketplace/#consume-from-any-assistant`) listing per-assistant install paths. Release-time gates `--check-versions` and `--check-clean` are opt-in: when present, they run after the build and exit non-zero on misalignment / drift (codes 3 and 4 respectively) so release pipelines can fail fast. When `apm.yml` declares `target: claude` or `target: copilot` (or the plural `targets:` equivalent), `apm pack` also generates an ecosystem-specific `plugin.json`: `.claude-plugin/plugin.json` for Claude (includes `mcpServers` from `.mcp.json` if present) and `.github/plugin/plugin.json` for Copilot (omits `mcpServers`). An existing file at the target path is preserved (a warning is emitted and the write is skipped) unless `--force` is passed; `--dry-run` prevents writes. Credential-bearing keys and secret-shaped values in `.mcp.json` are stripped recursively at any depth from the Claude manifest before writing, so a committed manifest never leaks secrets (see the apm pack reference, `reference/cli/pack/#credential-stripping-claude-mcpservers`). | `-o PATH`, `--archive` (produce a `.zip` archive instead of a directory; changed from `.tar.gz`), `--archive-format [zip\|tar.gz]` (default `zip`; use `tar.gz` for smaller legacy CI artifacts; only active with `--archive`), `--dry-run`, `--format [plugin\|apm]` (default `plugin`), `--force`, `--offline`, `--include-prerelease`, `--marketplace=FORMATS`, `--marketplace-path FORMAT=PATH`, `--json`, `--check-versions` (release gate: per-package versions match `marketplace.versioning.strategy`; exit 3 on failure), `--check-clean` (release gate: regenerate-and-diff against the committed `marketplace.json`; exit 4 on drift). `-t/--target` is **deprecated** (warn only). Exit codes: `0` success, `1` build/runtime error, `2` schema validation error, `3` `--check-versions` misalignment, `4` `--check-clean` drift. |
| `apm unpack BUNDLE` | **[Deprecated]** Extract a bundle. Use `apm install <bundle-path>` instead -- it deploys directly with integrity verification and target resolution. | `-o PATH`, `--skip-verify`, `--force`, `--dry-run` |

`apm install <BUNDLE-PATH>` -- when the positional argument resolves to a directory containing `plugin.json` at its root, or to a `.zip` (or legacy `.tar.gz`/`.tgz`) archive whose extracted root contains `plugin.json`, install switches to local-bundle mode: the bundle is integrity-verified against its embedded `apm.lock.yaml` (`pack.bundle_files`) and deployed into the consumer's resolved target. Target resolution follows the same precedence as registry installs (`--target` > `apm.yml` > directory detection); the bundle itself carries no target binding. Targets without target-native instruction deployment (opencode, codex, gemini) receive instructions staged under `apm_modules/<slug>/.apm/instructions/` and the install emits a hint to run `apm compile` to merge them. Other existing paths (e.g. a source-package directory without `plugin.json`) still flow through the normal local-path dependency-resolver pipeline. Files are recorded under `local_deployed_files` in the project lockfile -- `apm.yml` is **never** mutated. Honours `--target`, `--global`, `--force`, `--dry-run`, `--verbose`, plus `--as ALIAS` (log/display label only). Resolver/MCP/registry/policy flags (`--update`, `--mcp`, `--parallel-downloads`, `--allow-insecure-host`, `--skill`, ...) are rejected with a single consolidated error -- local-bundle install is an imperative deploy and bypasses those subsystems.

## Registry publishing (experimental)

Behind `apm experimental enable registries`. Pushes a package version to a REST-based APM registry declared in `apm.yml`'s `registries:` block.

| Command | Purpose | Key flags |
|---------|---------|-----------|
| `apm publish` | Auto-pack a flat registry zip archive (`apm.yml` + `.apm/` + `README.md`/`CHANGELOG.md`/`LICENSE` when present) and upload to a configured registry via `PUT /v1/packages/{owner}/{repo}/versions/{version}`. Different layout from `apm pack` (no `plugin.json` wrapper). | `--registry NAME` (required when multiple registries are configured), `--package OWNER/REPO` (**required** - registry package identity, e.g. `acme/my-skill`), `--zip PATH` (skip auto-pack and upload a pre-built `.zip`), `--dry-run`, `-v`/`--verbose` |

Examples:

```bash
# Auto-pack and publish when only one registry is configured
apm publish

# Choose a registry when multiple are configured, preview first
apm publish --registry corp-main --dry-run -v
apm publish --registry corp-main

# Publish a pre-built zip (skill-only or custom layout)
apm publish --zip ./build/my-package-1.0.0.zip --registry corp-main

# Specify registry package identity (required)
apm publish --package acme/my-package --registry corp-main
```

Exit codes: `0` published (or `--dry-run` ok), `1` publish failure (missing `apm.yml`/`.apm/`, auth `401`/`403`, version conflict `409`, server validation `422`, network/registry error, registries feature disabled), `2` usage error (cannot infer `owner/repo`, multiple registries without `--registry`, unknown `--registry` name, invalid flag combination).

Credentials resolve via `APM_REGISTRY_TOKEN_{NAME}` env var (or `apm config set registry.<name>.token`); see `authentication.md` for the full registry token chain.

## Marketplace (consumer)

| Command | Purpose | Key flags |
|---------|---------|-----------|
| `apm marketplace add SOURCE` | Register a marketplace. `SOURCE` accepts `OWNER/REPO`, `HOST/OWNER/REPO`, nested `HOST/group/sub/.../REPO`, HTTPS git URL with optional `#ref`, hosted `marketplace.json` URL, SSH URL (`git@host:org/repo.git`), local directory or file path, or `file://` URI. `--ref` applies only to git-backed sources; `--host` applies only to shorthand sources. | `-n NAME`, `-r REF`, `--host HOST` |
| `apm marketplace list` | List registered marketplaces | -- |
| `apm marketplace browse NAME` | Browse marketplace plugins | -- |
| `apm marketplace update [NAME]` | Update marketplace index | -- |
| `apm marketplace remove NAME` | Remove a marketplace | `-y` skip confirm |
| `apm marketplace validate NAME` | Validate marketplace manifest | `--check-refs`, `-v` |
| `apm search QUERY@MARKETPLACE` | Search marketplace | `--limit N` |
| `apm install NAME@MKT[#ref]` | Install from marketplace | Optional `#ref` override |
| `apm view NAME@MARKETPLACE` | View marketplace plugin info | -- |

## Marketplace authoring

> Source of truth is the `marketplace:` block in `apm.yml`. `apm pack` produces `.claude-plugin/marketplace.json` whenever that block is present. The legacy standalone `marketplace.yml` is deprecated -- use `apm marketplace migrate` to fold it in.

| Command | Purpose | Key flags |
|---------|---------|-----------|
| `apm marketplace init` | Append a `marketplace:` block to `apm.yml` and create `.claude-plugin/` | `--force`, `--no-gitignore-check`, `--name`, `--owner` |
| `apm marketplace migrate` | Fold a legacy `marketplace.yml` into `apm.yml`'s `marketplace:` block; deletes `marketplace.yml` on success | `--force`/`--yes`/`-y`, `--dry-run`, `-v` |
| `apm marketplace outdated` | Report upgradable plugins, range-aware; respects `tag_pattern` and common monorepo tag layouts | `--offline`, `--include-prerelease`, `-v` |
| `apm marketplace check` | Validate the `marketplace:` block and verify refs resolve | `--offline`, `-v` |
| `apm marketplace audit NAME` | Supply-chain audit: warn when plugin transitive deps bypass marketplace pinning | `--strict` (CI exit-1 on bypass), `-v` |
| `apm doctor` | Diagnose git, network, auth, marketplace config readiness, and (when a `marketplace:` block is present) **format coverage** -- which output profiles are configured vs. supported, so producers can spot easy reach wins (e.g. add `codex: {}` to also publish for Codex consumers). All marketplace-specific rows are informational and never affect exit code. | `-v` |
| `apm marketplace package add <source>` | Add a plugin entry to `marketplace.plugins` (source accepts `owner/repo` or `./path`) | `--name`, `--version`, `--ref` (mutable refs auto-resolved to SHA), `-d`/`--description`, `-s`/`--subdir`, `--tag-pattern`, `--tags`, `--include-prerelease`, `--no-verify` |
| `apm marketplace package set <name>` | Update fields on an existing plugin entry | `--version`, `--ref` (mutable refs auto-resolved to SHA), `--description`, `--subdir`, `--tag-pattern`, `--tags`, `--include-prerelease` |
| `apm marketplace package remove <name>` | Remove a plugin entry from `marketplace.plugins` | `--yes` |

To build the marketplace, run `apm pack` (it reads `apm.yml` and writes `.claude-plugin/marketplace.json` whenever the `marketplace:` block is present). `apm init --marketplace` is the equivalent shortcut at project-creation time -- it seeds a fresh `apm.yml` with the `marketplace:` block already in place.

## MCP servers

| Command | Purpose | Key flags |
|---------|---------|-----------|
| `apm mcp install NAME [-- CMD...]` | Add an MCP server (alias for `apm install --mcp`) | `--transport`, `--url`, `--env`, `--header`, `--mcp-version`, `--registry URL`, `--dev`, `--force`, `--dry-run` |
| `apm mcp list` | List MCP servers in project | `--limit N` |
| `apm mcp search QUERY` | Search MCP registry | `--limit N` |
| `apm mcp show SERVER` | Show server details | -- |

Self-defined stdio MCP entries declared in `apm.yml` (`env:` / `args:`) have their placeholders resolved at install time on Codex, Gemini, Antigravity, and Cursor, which have no runtime interpolation. Copilot CLI preserves env references as `${VAR}`; VS Code and JetBrains preserve them as `${env:VAR}`. All three env syntaxes are accepted: `${VAR}`, `${env:VAR}`, and the legacy `<VAR>`. Missing variables fall back to an interactive prompt on install-time targets (suppressed in non-TTY contexts). See [Manifest schema -- MCP placeholder syntaxes](https://microsoft.github.io/apm/reference/manifest-schema/) for the per-target matrix.

Set `MCP_REGISTRY_URL` (default `https://api.mcp.github.com`) to point all `apm mcp` commands and `apm install --mcp` at a custom MCP registry. The URL is validated at startup and must use `https://`; set `MCP_REGISTRY_ALLOW_HTTP=1` to opt in to plaintext `http://` for development. The registry must implement the [MCP Registry v0.1 spec](https://github.com/modelcontextprotocol/registry) (apm calls `/v0.1/servers/...`); legacy `/v0/`-only registries will return 404. When the override is set and the registry is unreachable during install pre-flight, APM fails closed.

## Runtime management (experimental)

| Command | Purpose | Key flags |
|---------|---------|-----------|
| `apm runtime setup {copilot\|codex\|llm\|gemini\|windsurf}` | Install a runtime. Codex verifies the GitHub Releases SHA-256 digest before extracting and fails on missing or mismatched digests. | `--version`, `--vanilla` |
| `apm runtime list` | Show installed runtimes | -- |
| `apm runtime remove {copilot\|codex\|llm\|gemini\|windsurf}` | Remove a runtime | `-y`, `--yes` |
| `apm runtime status` | Show active runtime | -- |

## Experimental features

| Command | Purpose | Key flags |
|---------|---------|-----------|
| `apm experimental` | Default to `apm experimental list` | `-v` verbose |
| `apm experimental list` | List registered experimental flags or emit JSON for automation | `--enabled`, `--disabled`, `--json`, `-v` verbose |
| `apm experimental enable NAME` | Enable an opt-in experimental flag | `-v` verbose |
| `apm experimental disable NAME` | Disable an opt-in experimental flag | `-v` verbose |
| `apm experimental reset [NAME]` | Reset one flag or all flags to defaults; also cleans malformed overrides during bulk reset | `-y` skip confirm, `-v` verbose |

Use `apm experimental enable copilot-cowork` to turn on Microsoft 365 Copilot Cowork skill deployment. Once enabled, deploy skills with `apm install --target copilot-cowork --global`.

Use `apm experimental enable copilot-app` to turn on GitHub Copilot desktop App workflow deployment. Once enabled, prompts that carry workflow frontmatter -- any flat top-level key of `interval`, `schedule_hour`, `schedule_day` -- can be deployed to the App's SQLite store at `~/.copilot/data.db` with `apm install --target copilot-app` (project scope) or `--target copilot-app --global` (user scope). A `.prompt.md` belongs to exactly ONE surface: workflow-shape prompts go to the App DB, plain prompts go to slash-command targets. Rows always start `enabled = 0` -- you opt in from the App. `apm install / update / uninstall` preserve user state (`enabled`, `last_run_at`, schedule overrides). Override the database path with `APM_COPILOT_APP_DB=<abs-path>`. Workflows are scoped to a real Copilot App project: when the App is running APM registers the project over the App's loopback WebSocket so the project is immediately known to the webview; when the App is closed APM falls back to a direct-SQLite `BEGIN IMMEDIATE` resolver. The first install in a brand-new repo prints a one-time "restart the Copilot App once" hint (see github/github-app#5483); subsequent installs are silent. `--global` installs that carry workflow-shape prompts warn-and-proceed because workflows run with `CWD=~/.copilot` rather than a repo -- attach the row to a project from the App's Workflows tab to fix.

Use `apm experimental enable openclaw` to turn on OpenClaw agent runtime skill deployment. Once enabled, deploy skills with `apm install --target openclaw` (project scope, `.agents/skills/`) or `apm install --target openclaw --global` (user scope, `~/.openclaw/skills/`). At project scope, output is identical to `agent-skills`; the `--global` user path is the distinguishing capability.

### Cross-client skills (`agent-skills`)

Use `--target agent-skills` to deploy skills to `.agents/skills/` -- the cross-tool standard directory. This is useful when multiple clients (Codex, future tools) read from `.agents/skills/`. Unlike `--target all`, `agent-skills` must be requested explicitly: `apm install --target agent-skills` or `apm install --target all,agent-skills` for both. `apm compile --target agent-skills` is a no-op (skills-only target).

> **Note:** `--target agents` is **deprecated** -- it maps to `copilot` (`.github/`), not `.agents/`. Use `--target copilot` or `--target agent-skills` instead.

### Skill routing convergence

By default, Copilot, Cursor, OpenCode, Codex, Gemini, and Antigravity all deploy skills to `.agents/skills/` (the agentskills.io standard). Claude is the only exception and retains its native per-client routing (`.claude/skills/`). Use `--legacy-skill-paths` (or `APM_LEGACY_SKILL_PATHS=1`) to restore the previous per-client layout (`.github/skills/`, `.cursor/skills/`, `.gemini/skills/`, etc.). Legacy per-client skill paths recorded in `apm.lock.yaml` are auto-migrated to `.agents/skills/` on the next `apm install`; foreign / hand-authored skills outside the lockfile are never touched.

Experimental flags MUST NOT gate security-critical behaviour (content scanning, path validation, lockfile integrity, token handling, MCP trust, collision detection). Flags are ergonomic/UX toggles only.

## Configuration and updates

| Command | Purpose | Key flags |
|---------|---------|-----------|
| `apm config` | Show current configuration | -- |
| `apm config get [KEY]` | Get one config value; with no key, prints the user config summary (effective values for core user-settable keys; omits noise-reduction defaults/unset optional values until configured) (`auto-integrate`, `target`, `self-update.channel`, `self-update.install-dir`, `temp-dir`, `allow-protocol-fallback`, `prefer-ssh`, `copilot-cowork-skills-dir`, `mcp-registry-url`) | -- |
| `apm config list` | Show the default user config listing; this aliases `apm config get` with no key and omits noisy defaults/unset optional values until configured | -- |
| `apm config set KEY VALUE` | Set a config value (`auto-integrate`, `target`, `self-update.channel`, `self-update.install-dir`, `temp-dir`, `allow-protocol-fallback`, `prefer-ssh`, `mcp-registry-url`; `copilot-cowork-skills-dir` requires `apm experimental enable copilot-cowork`) | -- |
| `apm config unset KEY` | Remove a stored config value (`target`, `self-update.channel`, `self-update.install-dir`, `temp-dir`, `allow-protocol-fallback`, `prefer-ssh`, `copilot-cowork-skills-dir`, `mcp-registry-url`) | -- |
| `apm lock` | Resolve all dependencies in `apm.yml` and write `apm.lock.yaml` **without** deploying any files to agent targets. Mirrors `cargo generate-lockfile` / `pnpm lock`. Use to bootstrap or refresh the lockfile before reviewing and applying changes. | `--update` re-resolve to latest SHAs, `--verbose`, `-g/--global`, `--no-policy`, `--target` (comma-separated), `--parallel-downloads N` |
| `apm lock export` | Export an SBOM/inventory from the **existing** `apm.lock.yaml` -- reads the lockfile only (no re-resolve, no re-hash, no network). Emits component identity (purl), recorded hashes, and the declared license. Output is deterministic (components sorted by purl, pinned timestamp) for byte-identical reproducibility. This is an inventory export, not a security attestation. | `-f/--format [cyclonedx\|spdx]` (default `cyclonedx`), `-o/--output FILE` (default stdout), `-g/--global` read user-scope lockfile, `--timestamp ISO8601` pin the document timestamp (falls back to `SOURCE_DATE_EPOCH`, then the lockfile's `generated_at`) |
| `apm update [PKGS...]` | Refresh APM dependencies: resolves `apm.yml` against the latest refs, prints a structured plan (added/updated/removed/unchanged), and prompts before mutating anything (default `[y/N]`). Full-SHA pins are resolved against the latest annotated semver tag, rewritten to that tag's SHA, and annotated as `# <tag>` in `apm.yml`. Pass `[PKGS...]` to refresh only those deps, or `-g` for user scope (`~/.apm/`). Strict superset of the deprecated `apm deps update`. Skips the prompt with `--yes`; previews with `--dry-run`. | `--yes`, `--dry-run`, `--verbose`, `-g/--global`, `--force`, `--parallel-downloads N`, `--target` (comma-separated) |
| `apm self-update` | Update the APM CLI itself (or show distributor guidance when self-update is disabled at build time). | `--check` only check |

`apm config set prefer-ssh true` and `apm config set allow-protocol-fallback true` persist transport preferences to `~/.apm/config.json` so SSH-only and corporate GHES users no longer need to re-pass `--ssh` / `--allow-protocol-fallback` on every `apm install`. Resolution order: CLI flag > `APM_GIT_PROTOCOL` / `APM_ALLOW_PROTOCOL_FALLBACK` env var > `apm config` value > built-in default (`false`). `apm config unset prefer-ssh` and `apm config unset allow-protocol-fallback` remove the persisted value. In `apm config` / `apm config list` / `apm config get` (no key), the two transport rows surface only when they have been enabled (the `false`-default rows are suppressed to keep the output noise-free); `apm config get <key>` always returns the effective value. Setting `allow-protocol-fallback=true` while `CI=1` emits a warning because the persisted value affects every subsequent `apm install` on a shared `$HOME`; prefer the env var in CI.

`apm config set target <value>` persists a default install target (single token or comma-separated list) for `apm install` when both `--target` and `apm.yml target(s)` are absent. `apm config unset target` removes this fallback.

`apm config set self-update.channel stable|prerelease` and `apm config set self-update.install-dir <path>` persist non-secret installer defaults for `apm self-update`. Environment variables win: `VERSION` pins an exact release, `APM_SELF_UPDATE_CHANNEL` overrides the channel, and `APM_INSTALL_DIR` overrides the install directory. Self-update config deliberately rejects credentials, tokens, mirror URLs, commands, and installer args; credentials stay on the existing auth path and enterprise mirror URLs stay env-only.

`apm self-update` shares the Windows installer codepath used by `install.ps1`: it stages the new release under `%LOCALAPPDATA%\Programs\apm\releases\<tag>` before running `apm.exe --version`, so an AppLocker / WDAC allow-list rule for `%LOCALAPPDATA%\Programs\apm\*` suffices. When the smoke test fails with HRESULT `0x80070005` (`Access is denied`), the installer emits a specific AppLocker/WDAC diagnostic with three remediations (allow-list rule, set `APM_TEMP_DIR` to an allow-listed path, or fall back to `pip install --user apm-cli`) instead of silently retrying via pip.

`apm self-update` (and the startup version-checker) honours the same env vars as `install.sh` for air-gapped and GitHub Enterprise Server (GHE) environments: `GITHUB_URL` overrides the GitHub base URL and API host (`{GITHUB_URL}/api/v3` for GHE), `APM_REPO` overrides the repository (default `microsoft/apm`), and `VERSION` pins a release and skips the GitHub API call entirely. Example: `GITHUB_URL=https://gh.corp.com APM_REPO=corp/apm VERSION=v1.2.3 apm self-update`.

`apm config set copilot-cowork-skills-dir <absolute-path>` persists the Cowork skills directory across shells. `apm config get copilot-cowork-skills-dir` and `apm config unset copilot-cowork-skills-dir` remain available even when the `copilot-cowork` flag is disabled so leftover state can still be inspected or cleared. In `apm config` and bare `apm config get`, the `copilot-cowork-skills-dir` entry is shown only when the `copilot-cowork` flag is enabled.

`apm config set external.<name>.llm true|false` and `apm config set external.<name>.args -- "<flags>"` persist per-scanner external-scanner defaults to `~/.apm/config.json` (JSON section `external_scanners.<name>.{llm,args}`), behind `apm experimental enable external-scanners`. `<name>` is validated against the supported scanners (e.g. `skillspector`). `.args` is shlex-split and stored as a list; use the `--` separator so Click does not parse a leading `--flag` as an option. `apm config get external.<name>.{llm,args}`, `apm config unset external.<name>.{llm,args}`, and `apm config unset external.<name>` (removes both) round out the surface. These keys are reachable only when the flag is enabled; bare `apm config get` lists set external keys when the flag is on. CLI flags (`--external-llm`, `--external-args`) override these values for a single run.

`apm config set mcp-registry-url https://mcp.internal.example.com` persists a private MCP registry URL so users do not need to export `MCP_REGISTRY_URL` every session. Accepts `http://` or `https://` URLs; all other schemes are rejected. Resolution order: `--registry <url>` flag on `apm mcp install` / `apm install --mcp` > `MCP_REGISTRY_URL` env var > `mcp-registry-url` in `~/.apm/config.json` > built-in public default. When the config layer is active, `apm mcp search` prints a `Registry (config): <url>` diagnostic. `apm config unset mcp-registry-url` removes the persisted URL.

`apm approve [PACKAGE_REF...]` grants a package permission to deploy executable primitives (hooks, `bin/`, self-defined MCP servers, canvas extensions). By default it writes the project `apm.yml` `executables.allow` block -- committed to source control, so the whole team inherits the trust decision. `apm approve --user` records a personal grant in `~/.apm/config.json` instead (machine-local, never committed, lowest authority -- it can only narrow trust).

The project `apm.yml` must carry an `executables:` block (even empty `{}`) to enable the gate; without it, all executables are allowed unconditionally. Flags: `--pending` lists packages with unapproved executables; `--all` approves every currently blocked package; `--recommended` bulk-accepts the org `executables.recommend` set; `--list` shows the fleet-level effective trust decision and deciding layer per installed package.

`apm deny [PACKAGE_REF...]` writes a block to the project `executables.deny` (or `~/.apm/config.json` with `--user`); deny always wins, and denying a not-yet-installed package is allowed (a pre-emptive block). `apm policy explain <PACKAGE_REF>` (a subcommand of the `apm policy` group, sibling to `apm policy status`) prints the effective decision for a package: allowed or blocked per type, the deciding policy layer, and any shadowed (overridden) layers. `apm doctor` adds a fleet-level executable-trust drift check that flags packages allowed locally but denied by org policy.

The legacy top-level `allowExecutables:` block is a deprecated alias for `executables.allow`, read for one minor cycle and migrated on the next approve/deny write; the standalone `~/.apm/approvals.yml` is removed and migrated into `~/.apm/config.json` on first read. Grant keys are package-scoped in v1: `owner/repo` and `owner/repo#version` both match the package regardless of installed version. In CI, pre-approve packages by committing them to `executables.allow`; untrusted required executables fail `apm audit` with `required-executable-untrusted`.
