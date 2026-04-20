# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).


## [Unreleased]

### Changed (BREAKING)

- Strict-by-default transport selection: explicit `ssh://`/`https://` URLs no longer silently fall back to the other protocol; shorthand consults `git config url.<base>.insteadOf` and otherwise defaults to HTTPS. Set `APM_ALLOW_PROTOCOL_FALLBACK=1` (or pass `--allow-protocol-fallback`) to restore the legacy permissive chain; cross-protocol retries then emit a `[!]` warning. Closes #328 (#778)

### Added

- `apm install --ssh` / `--https` flags and `APM_GIT_PROTOCOL=ssh|https` env to pick the initial transport for shorthand dependencies (#778)
- `apm install --allow-protocol-fallback` flag and `APM_ALLOW_PROTOCOL_FALLBACK=1` env as the migration escape hatch for cross-protocol fallback (#778)
- Add APM Review Panel skill (`.github/skills/apm-review-panel/`) and four new specialist personas (`devx-ux-expert`, `supply-chain-security-expert`, `apm-ceo`, `oss-growth-hacker`) with auto-activating per-persona skills. Routes specialist findings through an APM CEO arbiter for strategic / breaking-change calls, with the OSS growth hacker side-channeling adoption insights via `WIP/growth-strategy.md`. Instrumentation per Handbook Ch. 9 (`The Instrumented Codebase`); PROSE-compliant (thin SKILL.md routers, persona detail lazy-loaded via markdown links, explicit boundaries per persona).

### Fixed

- Preserve custom git ports across protocols: non-default ports on `ssh://` and `https://` dependency URLs (e.g. Bitbucket Datacenter on SSH port 7999, self-hosted GitLab on HTTPS port 8443) are now captured as a first-class `port` field on `DependencyReference` and threaded through all clone URL builders. When the SSH clone fails, the HTTPS fallback reuses the same port instead of silently dropping it (#661, #731)

## [0.8.12] - 2026-04-19

### Added

- `apm install` now automatically discovers and deploys local `.apm/` primitives (skills, instructions, agents, prompts, hooks, commands) to target directories, with local content taking priority over dependencies on collision (#626, #644)
- Deploy primitives from the project root's own `.apm/` directory alongside declared dependencies, so single-package projects no longer need a sub-package stub to install their own content (#715)
- Add `temp-dir` configuration key (`apm config set temp-dir PATH`) to override the system temporary directory, resolving `[WinError 5] Access is denied` in corporate Windows environments (#629)

### Changed

- Refactor `apm install` into a modular engine package (`apm_cli/install/`) with discrete phases (resolve, targets, download, integrate, cleanup, lockfile, finalize, post-deps local) and apply design patterns -- introduce a `DependencySource` Strategy hierarchy with shared `run_integration_template()` Template Method (kills ~300 LOC duplication across local/cached/fresh dep handlers), add `services.py` DI seam to eliminate `_install_mod` indirection, and wrap the pipeline in a typed `InstallService` Application Service consuming a frozen `InstallRequest`. `install/phases/integrate.py` shrinks from 1013 to ~400 LOC; the public `apm install` behaviour and CLI surface are unchanged. Preserves the `#762` cleanup chokepoint and remains backward-compatible (`_install_apm_dependencies` re-export and 55 healthy test patches keep working) (#764)
- `apm marketplace browse/search/add/update` now route through the registry proxy when `PROXY_REGISTRY_URL` is set; `PROXY_REGISTRY_ONLY=1` blocks direct GitHub API calls (#506, #617)
- CI: adopt GitHub Merge Queue with tiered CI and add an inert `pull_request_target` stub workflow (`ci-integration-pr-stub.yml`) for the four Tier 2 required checks. `ci.yml` (Tier 1: unit tests + binary build) now runs on `pull_request` and `merge_group`. The integration + release-validation suite (Tier 2) moves to `merge_group`-only, replacing the previous `workflow_run` + environment-approval flow. PR branches no longer need manual updates against `main`, and the heavy integration suite runs once at merge time instead of on every PR push. The PR-time stub satisfies branch protection's required-check gate without burning CI minutes, holds no secrets, runs no checkout, and grants `permissions: {}` so it cannot be turned into a supply-chain attack vector. CODEOWNERS now requires Lead Maintainer review for any change to `.github/workflows/**` (#770, #771)
- Bump `pytest` from 8.4.2 to 9.0.3 (#698)
- Bump `dompurify` from 3.3.2 to 3.4.0 in `/docs` (#730)
- Bump `lodash-es` and `langium` in `/docs` (#761)
- Add `.editorconfig` to standardize charset, line endings, indentation, and trailing whitespace across contributions (#671)
- Add `@sergio-sisternes-epam` as maintainer (#623)
- Close install/uninstall/update CLI integration coverage gaps surfaced by the `#764` review (#767)
- Add 55 unit tests for `commands/deps/_utils.py` and `commands/view.py` to address Test Improver backlog items #4 and #5 (#682)

### Fixed

- Harden `apm install` stale-file cleanup to prevent unsafe lockfile deletions, preserve user-edited files via per-file SHA-256 provenance, and improve cleanup reporting during install and `--dry-run` (#666, #750, #762)
- Local `.apm/` stale-cleanup now uses pre-install content hashes for provenance verification. Previously the lockfile was re-read after regeneration, which always yielded empty hashes, causing the user-edit safety gate to be silently skipped for project-local files (#764)
- Fix `apm install --target claude` not creating `.claude/` when the directory does not already exist (`auto_create=False` targets now get their root directory created when explicitly requested) (#763)
- Fix content hash mismatch on re-install when `.git/` is absent from installed packages by falling back to content-hash verification before re-downloading (#763)
- Make `apm install` idempotent for hook entries: upsert by `_apm_source` ownership marker instead of unconditionally appending, so re-running install no longer duplicates per-event hook commands (#709)
- Rewrite Windows backslash paths in hook commands' `windows` key during integration; previously only Unix-style `./` references were rewritten, leaving `windows` script paths unresolved at runtime (#609)
- Add explicit `encoding="utf-8"` to `.prompt.md` `open()` calls in `script_runner` to prevent `UnicodeDecodeError` on Windows non-UTF-8 locales (CP950/CP936/CP932) (#607)
- Validate the `project_name` argument to `apm init` and reject `/` and `\` to prevent confusing `[WinError 3]` and silent path-traversal behaviour (#724)
- Use `yaml.safe_dump` when generating `apm.yml` for virtual-file and collection packages, so `description` values containing `:` no longer break `apm install` with a YAML parse error (#707)
- `_count_package_files` in `apm deps list` now reads the canonical `.apm/context/` (singular) directory; previously it scanned `.apm/contexts/` and always reported `0 context files` per package (#748)
- `apm pack --format plugin` no longer emits duplicated `skills/skills/` nesting for bare-skill dependencies referenced through virtual paths like `skills/<name>` (#738)
- Provide an ADO-specific authentication error message for `dev.azure.com` remotes so users get actionable guidance instead of a generic GitHub-flavored hint (#742)
- Fix `apm compile --target codex` (and `opencode`, `minimal`) being a silent no-op; `AgentsCompiler.compile()` now routes these through the AGENTS.md compiler instead of returning an empty success result that left stale `AGENTS.md` files (#766)
- Support `codeload.github.com`-style archive URLs in Artifactory archive URL generation, unblocking JFrog Artifactory proxies configured against `codeload.github.com` (#712)
- `_parse_artifactory_base_url()` now reads `PROXY_REGISTRY_URL` first (with `ARTIFACTORY_BASE_URL` fallback + `DeprecationWarning`), and the virtual-subdirectory download path checks `dep_ref.is_artifactory()` before falling back to env-var detection, fixing lockfile reinstall failures when proxy config is only on the lockfile entry (#616)
- Fall back to SSH URLs when validating git remotes for generic / self-hosted hosts so `apm install` no longer fails the pre-install validation step against private SSH-only servers (#584)
- Suppress internal config keys (e.g. `default_client`) from `apm config get` output, removing the get/set asymmetry that confused users and was flagged as a Medium security issue (#571)
- Include dependency instructions stored in `.github/instructions/` (not only `.apm/instructions/`) when running `apm compile --target claude` without `--local-only` (#631, #642)
- Fix `apm marketplace add` silently failing for private repos by using credentials when probing `marketplace.json` (#701)
- Harden marketplace plugin normalization to enforce that manifest-declared `agents`/`skills`/`commands`/`hooks` paths resolve inside the plugin root (#760)
- Pin codex setup to `rust-v0.118.0` for security and reproducibility; update config to `wire_api = "responses"` (#663)
- Propagate headers and environment variables through OpenCode MCP adapter with defensive copies to prevent mutation (#622)
- Fix `apm install` hanging indefinitely when corporate firewalls silently drop SSH packets by setting `GIT_SSH_COMMAND` with `ConnectTimeout=30` (#652, #653)
- Stop `test_auto_detect_through_proxy` from making real `api.github.com` calls by passing a mock `auth_resolver`, fixing flaky macOS CI rate-limit failures (#759)
- Fix the Daily Test Improver workflow creating duplicate monthly activity issues; Task 7 now finds and updates the existing month's issue instead of opening a new one each run (#681)

## [0.8.11] - 2026-04-06

### Added

- Artifactory archive entry download for virtual file packages (#525)
- `apm view <package> [field]` command for viewing package metadata and remote refs (#613)
- `apm view <package> versions` field selector lists remote tags and branches via `git ls-remote` (#613)
- `apm outdated` command compares locked dependencies against remote refs (#613)
- `--parallel-checks` (`-j`) option on `apm outdated` for concurrent remote checks (default: 4) (#613)
- Rich progress feedback during `apm outdated` dependency checking (#613)
- `--global` flag on `apm view` for inspecting user-scope packages (#613)

### Changed

- Rename `apm info` to `apm view` for npm convention alignment; `apm info` kept as hidden alias (#613)
- Scope resolution now happens once via `TargetProfile.for_scope()` and `resolve_targets()` -- integrators no longer need scope-aware parameters (#562)
- Unified integration dispatch table in `dispatch.py` -- both install and uninstall import from one source of truth (#562)
- Hook merge logic deduplicated: three copy-pasted JSON-merge methods replaced with `_integrate_merged_hooks()` + config dict (#562)
- `apm outdated` uses SHA comparison for branch-pinned deps instead of reporting them as `unknown` (#613)

### Fixed

- Reject symlinked primitive files in all discovery and resolution paths to prevent symlink-based traversal attacks (#596)
- `apm install -g` now deploys hooks to the scope-resolved target directory instead of hardcoding `.github/hooks/` (#565, #566)
- Hook sync/cleanup derives prefixes dynamically from `KNOWN_TARGETS` instead of hardcoded paths (#565)
- `auto_create=False` targets no longer get directories unconditionally created during install (#576)
- `apm deps update -g` now correctly passes scope, preventing user-scope updates from silently using project-scope paths (#562)
- Subprocess encoding failures on Windows non-UTF-8 consoles (CP950/CP936) -- all subprocess calls now use explicit UTF-8 encoding (#591)
- PowerShell 5.1 compatibility: replace multi-argument `Join-Path` calls with nested two-argument calls (#593)
- `apm marketplace add` now respects `GITHUB_HOST` environment variable for GitHub Enterprise users (#589)
- `compilation.exclude` patterns now filter primitive discovery, preventing excluded files from leaking into compiled output (#477)
- Runtime detection in script runner now uses anchored patterns to prevent false positives when runtime keywords appear in flag values (#563)
- `apm compile` now warns when instructions are missing `applyTo` across all compilation modes (#449)
- Detect remote default branch instead of hardcoding `main` (#574)
- Warn when two packages deploy a native skill with the same name (#545)

## [0.8.10] - 2026-04-03

### Fixed

- Hook integrator now processes the `windows` property in hook JSON files, copying referenced scripts and rewriting paths during install/compile (#311)
- Standardized `--target` choices, replaced Unicode with ASCII for cp1252 compatibility, and documented missing CLI flags (#519)
- `apm install -g` now correctly deploys to user-scope directories, skips unsupported primitives, and cleans up on uninstall -- including multi-level paths like `~/.config/opencode/` (#542)
- `apm deps update` now correctly re-resolves transitive dependencies instead of reusing stale locked SHAs (#548)

### Added

- `apm install` now deploys `.instructions.md` files to `.claude/rules/*.md` for Claude Code, converting `applyTo:` frontmatter to Claude's `paths:` format (#516)

### Changed

- Artifactory virtual file downloads now use the Archive Entry Download API to fetch individual files without downloading the full archive; falls back to full-archive extraction when the entry API is unavailable (#525)

## [0.8.9] - 2026-03-31

### Fixed

- `apm install NAME@MARKETPLACE` now respects `metadata.pluginRoot` from marketplace manifests, fixing resolution of bare-name plugins in marketplaces like `awesome-copilot` (#512)
- Windows unit test assertion tolerates Rich console line-wrapping on long temp paths (#510)
- Release validation scripts match updated `apm deps list` scope output (#510)

## [0.8.8] - 2026-03-31

### Added

- `apm install -g/--global` for user-scope package installation with per-target support matrix and `apm uninstall -g` lifecycle (#452)
- Marketplace integration: `apm install NAME@MARKETPLACE` syntax, `apm marketplace add/list/browse/update/remove`, `apm search` across registered marketplaces (#503)
- Codex as integration target: skills to `.agents/skills/`, agents to `.codex/agents/*.toml`, hooks to `.codex/hooks.json`, `--target codex` on install/compile/pack (#504)
- Lockfile-driven reproducible installs for registry proxies with `content_hash` verification and `RegistryConfig` -- by @chkp-roniz (#401)

### Changed

- `apm deps update` skips download when resolved SHA matches lockfile SHA, making the common "nothing changed" case near-instant (#500)

### Fixed

- `apm install -g ./local-pkg` rejects local path dependencies at user scope with a clear error (#452)
- Orphan documentation pages (`ci-policy-setup`, `policy-reference`) added to sidebar navigation; stale GitHub Rulesets content updated (#505, #507)

## [0.8.7] - 2026-03-30

### Fixed

- `--target opencode` no longer writes prompts/agents to `.github/`; dispatch loop now only fires primitives declared by the selected target (#488, #494)
- `--target cursor` now correctly deploys skills to `.cursor/skills/` instead of `.github/skills/` -- `SkillIntegrator` respects the explicit target list end-to-end (#482, #494)
- Misleading "transitive dep" error message for direct dependency download failures (#478)
- Sparse checkout using global token instead of per-org token from `GITHUB_APM_PAT_<ORG>` (#478)
- Duplicate error count when a dependency fails during both resolution and install phases (#478)
- Windows Defender false-positive (`Trojan:Win32/Bearfoos.B!ml`) mitigation: embed PE version info in Windows binary and disable UPX compression on Windows builds -- by @sergio-sisternes-epam (#490)
- `apm deps update` was a no-op -- rewrote to delegate to the install engine so lockfile, deployed files, and integration state are all refreshed correctly -- by @webmaxru (#493)

### Changed

- Integration dispatch is now data-driven: `KNOWN_TARGETS` defines each target's primitives and directory layout; adding a target requires zero code changes (#494)
- `partition_managed_files()` uses O(1) component-based path routing instead of linear prefix scan (#494)
- Uninstall sync uses pre-partitioned buckets via `partition_bucket_key()` instead of re-scanning the full managed-files set (#494)

### Security

- Bump `pygments` from 2.19.2 to 2.20.0 (#495)

## [0.8.6] - 2026-03-27

### Added

- `apm install --target` flag to force deployment to a specific target (copilot, claude, cursor, opencode, all) (#456)
- Global `apm install --global` / `-g` and `apm uninstall --global` flags for user-scope package installation, backed by `InstallScope`-based scope resolution in `core/scope.py`; deploys primitives to `~/.copilot/`, `~/.claude/`, `~/.cursor/`, `~/.config/opencode/` and tracks metadata under `~/.apm/` (#452)

### Fixed

- Windows antivirus file-lock errors (`WinError 32`) during `apm install` with `file_ops` retry utility (#440)
- Installer fallback to pip in devcontainers, target registry, and lockfile idempotency fixes (#456)
- Reject path traversal sequences in SSH-style Git URLs — by @thakoreh (#458)
- Exclude bundled OpenSSL libs from Linux binary to prevent ABI conflicts (#466)
- Allow spaces in ADO repository names when parsing URLs (#437)
- Gate `.claude/commands/` deployment behind `integrate_claude` flag (#443)
- Sort instruction discovery order for deterministic Build IDs across platforms (#468)
- Share `AuthResolver` across install to prevent duplicate auth popups (#424)

### Changed

- Consolidated path-segment traversal checks into `validate_path_segments()` in `path_security.py` (#458)

## [0.8.5] - 2026-03-24

### Added

- `apm audit --ci` org-level policy engine -- experimental Phase 1 for enterprise governance over agents in the SDLC; GitHub / GitHub Enterprise only (#365)
- `-v` shorthand for `--verbose`, `show_default` on boolean options, `deps clean --dry-run`/`--yes` flags (#303)
- `--verbose` on `apm install` now shows auth source diagnostics for virtual package validation failures (#414)
- Nightly runtime inference tests decoupled from release pipeline via `ci-runtime.yml` (#407)

### Changed

- CI pipeline optimization: merged test+build jobs, macOS as root nodes, native `setup-uv` caching, removed unnecessary `setup-node` steps (#407)
- Encoding instructions enforce ASCII-only source and CLI output with bracket status symbols (#282)

### Fixed

- Windows path hardening: `portable_relpath()` utility, ~23 `relative_to()` call-site migrations, CI lint guard (#411, #422)
- Centralized YAML I/O with UTF-8 encoding via `yaml_io` helpers, preventing Windows cp1252 mojibake, based on prior work by @alopezsanchez (#433, #388)
- SSL certificate verification in PyInstaller binary via `certifi` runtime hook (#429)
- `apm pack --target claude` cross-target path mapping for skills/agents installed under `.github/` (#426)
- `ARTIFACTORY_ONLY` enforcement for virtual package types (files, collections, subdirectories) (#418)
- Local path install: descriptive failure messages and Windows drive-letter path recognition (#431, #435)
- Windows test fixes in config command and agents compiler (#410)
- Removed stale WIP folder from tracking, strengthened `.gitignore` (#420)

## [0.8.4] - 2026-03-22

### Added

- Centralized `AuthResolver` with per-(host, org) token resolution, cached and thread-safe — replaces 4 scattered auth implementations (#394)
- `CommandLogger` and `InstallLogger` base classes for structured CLI output with validation, resolution, and download phases (#394)
- `--verbose` flag on `uninstall`, `pack`, and `unpack` commands (#394)
- Verbose output: dependency tree resolution, auth source/type per download, lockfile SHA, package type, inline per-package diagnostics (#394)
- Parent chain breadcrumb in transitive dependency error messages — "root-pkg > mid-pkg > failing-dep" (#394)
- `DiagnosticCollector.count_for_package()` for inline per-package verbose hints (#394)
- Auth flow diagram and package source behavior matrix in authentication docs (#394)
- Documented `${input:...}` variable support in `headers` and `env` MCP server fields (#349)

### Changed

- All CLI output now uses ASCII symbols (`[+]`, `[x]`, `[!]`) instead of Unicode characters (#394)
- Migrated `_rich_*` calls to `CommandLogger` across install, compile, uninstall, audit, pack, and bundle modules (#394)
- Verbose ref display uses clean `#tag @sha` format instead of nested parentheses (#394)
- Integration tree lines (`└─`) no longer have `[i]` prefix — clean visual hierarchy (#394)
- Global env vars (`GITHUB_APM_PAT`, `GITHUB_TOKEN`, `GH_TOKEN`) apply to all hosts — HTTPS is the security boundary, not host-gating (#394)
- Credential-fill timeout increased from 5s to 60s (configurable via `APM_GIT_CREDENTIAL_TIMEOUT`, max 180s) — fixes Windows credential picker timeouts (#394)

### Fixed

- Bundle lockfile includes non-target `deployed_files` causing `apm unpack` verification failure when packing with `--target` (#394)
- Verbose lockfile iteration crashed with `'str' object has no attribute 'resolved_commit'` (#394)
- CodeQL incomplete URL substring sanitization in test assertions (#394)

### Security

- Bumped `h3` from 1.15.6 to 1.15.9 in docs (#400)

### Removed

- Unused image files: `copilot-banner.png`, `copilot-cli-screenshot.png` (#391)

## [0.8.3] - 2026-03-20

### Added

- Plugin authoring — `apm pack --format plugin` exports APM packages as standalone plugin directories (`plugin.json`, agents, skills, commands) consumable by Copilot CLI, Claude Code, and Cursor without APM installed (#379)
- `apm init --plugin` scaffolds a hybrid project with both `apm.yml` and `plugin.json`, including a `devDependencies` section (#379)
- `devDependencies` in `apm.yml` — dev deps install normally but are excluded from `apm pack` output; `apm install --dev` writes to the dev section (#379)
- VS Code runtime detection now falls back to `.vscode/` directory presence when the `code` binary is not on PATH — by @sergio-sisternes-epam (#359)

### Security

- Content integrity hashing — SHA-256 `content_hash` per dependency in `apm.lock.yaml`, verified on subsequent installs to detect tampering or force-pushed commits (#315, #379)
- `apm audit --strip` now preserves a leading BOM while stripping suspicious mid-file BOMs, preventing false negatives — by @dadavidtseng (#372)

### Changed

- Install URLs now use short `aka.ms/apm-unix` and `aka.ms/apm-windows` redirects across README, docs, and CLI output (#384)
- README highlights link to relevant docs pages; plugin authoring featured as a key value proposition (#385)

### Fixed

- `DependencyReference` preserved through the download pipeline so lockfile records the original ref, not an empty object — by @sergio-sisternes-epam (#383)
- Refactor command and model modules for readability and maintainability — by @sergio-sisternes-epam (#232)
- CLI docs align `compile --target opencode`, `audit --dry-run`, and planned `audit --drift` with current behavior (#373)

## [0.8.2] - 2026-03-19

### Added

- JFrog Artifactory VCS repository support — explicit FQDN, transparent proxy via `ARTIFACTORY_BASE_URL`, and air-gapped `ARTIFACTORY_ONLY` mode (#354)
- GH-AW compatibility gate in release pipeline — `gh-aw-compat` job tests tokenless install + pack before publishing (#356)
- Release validation now includes `test_ghaw_compat` scenario (#356)

### Fixed

- Credential fill returning garbage token in tokenless CI environments — broke `apm install` for public repos in GitHub Actions (#356)

### Security

- Harden dependency path validation — reject invalid path segments at parse time, enforce install-path containment, safe deletion wrappers across `uninstall`, `prune`, and `install` (#364)

## [0.8.1] - 2026-03-17

### Added

- Audit hardening — `apm unpack` content scanning, SARIF/JSON/Markdown `--format`/`--output` for CI capture, `SecurityGate` policy engine, non-zero exits on critical findings (#330)
- Install output now shows resolved git ref alongside package name (e.g. `✓ owner/repo#main (a1b2c3d4)`) (#340)
- `${input:...}` variable resolution for self-defined MCP server headers and env values — by @sergio-sisternes-epam (#344)

### Changed

- Pinning hint moved from inline tip to `── Diagnostics ──` section with aggregated count (#347)
- Install ref display uses `#` separator instead of `@` for consistency with dependency syntax (#340)
- Shorthand `@alias` syntax removed from dependency strings — use the dict format `alias:` field instead (#340)

### Fixed

- File-level downloads from private repos now use OS credential helpers (macOS Keychain, `gh auth login`, Windows Credential Manager) (#332)
- Lockfile now preserves the host for GitHub Enterprise custom domains so subsequent `apm install` clones from the correct server (#338)
- MCP registry validation no longer fails on transient network errors (#337)

## [0.8.0] - 2026-03-16

### Added

- Native Cursor IDE integration — `apm install` deploys instructions→rules (`.mdc`), agents, skills, hooks (`hooks.json`), and MCP (`mcp.json`) to `.cursor/` (#301)
- Native OpenCode integration — `apm install` deploys agents, commands, skills, and MCP (`opencode.json`) to `.opencode/` — inspired by @timvw (#306)
- Content security scanning with `apm audit` command — `--file`, `--strip`, `--dry-run`; install-time pre-deployment gate blocks critical hidden Unicode characters (#313)
- Detect variation selectors (Glassworm attack vector), invisible math operators, bidi marks, annotation markers, and deprecated formatting in content scanning — by @raye-deng (#321, #320)
- Context-aware ZWJ detection — emoji joiners preserved by `--strip`; `--strip --dry-run` preview mode (#321)
- `TargetProfile` data layer for scalable multi-target architecture (#301)
- `CursorClientAdapter` for MCP server management in `.cursor/mcp.json` (#301)
- `OpenCodeClientAdapter` for MCP server management in `opencode.json` (#306)
- Private packages guide and enhanced authentication documentation (#314)

### Changed

- Updated docs landing page to include Cursor and OpenCode (#310)
- Updated all doc pages to reflect full Cursor native support (#304)
- Added OpenCode to README headline and compile description (#308)

### Fixed

- GitHub API rate-limit 403 responses no longer misdiagnosed as authentication failures — unauthenticated users now see actionable "rate limit exceeded" guidance instead of misleading "private repository" errors

## [0.7.9] - 2026-03-13

### Added

- Local filesystem path dependencies — install packages from relative/absolute paths with `apm install ./my-package` (#270)
- Windows native support (Phase 1 & 2) — cross-platform runtime management, PowerShell helpers, and CI parity — by @sergio-sisternes-epam (#227)
- CLI logging UX agent skill for consistent CLI output conventions (#289)

### Fixed

- Resolve `UnboundLocalError` in `apm prune` crashing all prune operations (#283)
- Restore CWD before `TemporaryDirectory` cleanup on Windows — by @sergio-sisternes-epam (#281)
- Fix Codex runtime download 404 on Windows — asset naming uses `.exe.tar.gz` — by @sergio-sisternes-epam (#287)
- Fix `UnicodeEncodeError` on Windows cp1252 consoles via UTF-8 codepage configuration — by @sergio-sisternes-epam (#287)
- Fix `WinError 2` when resolving `.cmd`/`.ps1` shell wrappers via `shutil.which()` — by @sergio-sisternes-epam (#287)
- Fix `GIT_CONFIG_GLOBAL=NUL` failure on some Windows git versions — by @sergio-sisternes-epam (#287)
- Improve sub-skill overwrite UX with content skip and collision protection (#289)

### Changed

- Lockfile renamed from `apm.lock` to `apm.lock.yaml` for IDE syntax highlighting; existing `apm.lock` files are automatically migrated on the next `apm install` (#280)
- Add Windows as first-class install option across documentation site (#278)
- Clarify that `.github/` deployed files should be committed (#290)

## [0.7.8] - 2026-03-13

### Added

- Diff-aware `apm install` — manifest as source of truth: removed packages, ref/version changes, and MCP config drift in `apm.yml` all self-correct on the next `apm install` without `--update` or `--force`; introduces `drift.py` with pure helper functions (#260)
- `DiagnosticCollector` for structured install diagnostics (#267)
- Detailed file-level logging to `apm unpack` command (#252)
- Astro Starlight documentation site with narrative redesign (#243)

### Fixed

- Resolve WinError 32 during sparse-checkout fallback on Windows — by @JanDeDobbeleer (#235)
- CLI consistency: docs alignment, emoji removal, `show_default` flags (#266)

### Changed

- Minimum Python version bumped to 3.10; Black upgraded to 26.3.1 (#269)
- Refactor `cli.py` and `apm_package.py` into focused modules — by @sergio-sisternes-epam (#224)
- Revamp README as storefront for documentation site (#251, #256, #258)
- Remove duplicated content from CLI reference page (#261)
- Bump devalue 5.6.3 → 5.6.4 in docs (#263)
- Primitives models coverage 78% → 100%; add discovery and parser coverage tests (#240, #254)

## [0.7.7] - 2026-03-10

### Added

- `copilot` as the primary user-facing target name for GitHub Copilot / Cursor / Codex / Gemini output format; `vscode` and `agents` remain as aliases (#228)

### Changed

- Consolidate pack/unpack documentation into cli-reference, rename Key Commands section

## [0.7.6] - 2026-03-10

### Added

- `apm pack` and `apm unpack` commands for portable bundle creation and extraction with target filtering, archive support, and verification (#218)
- Plugin MCP Server installation — extract, convert, and deploy MCP servers defined in plugin packages (#217)

### Fixed

- Plugin agents not deployed due to directory nesting in custom agent paths (#214)
- Skip already-configured self-defined MCP servers on re-install (#191)
- CLI consistency: remove emojis from help strings, fix `apm config` bare invocation, update descriptions (#212)

### Changed

- Extract `MCPIntegrator` from `cli.py` — move MCP lifecycle orchestration (~760 lines) into standalone module with hardened error handling (#215)

## [0.7.5] - 2026-03-09

### Added

- Plugin management system with CLI commands for installing and managing plugins from marketplaces (#83)
- Generic git URL support for GitLab, Bitbucket, and any self-hosted git provider (#150)
- InstructionIntegrator for `apm install` — deploy `.instructions.md` files alongside existing integrators (#162)
- Transitive MCP dependency propagation (#123)
- MCP dependency config overlays, transitive trust flag, and related bug fixes (#166)
- Display build commit SHA in CLI version output (#176)
- Documentation: apm.yml manifest schema reference for integrators (#186)

### Fixed

- Handle multiple brace groups in `applyTo` glob patterns (#155)
- Replace substring matching with path-component matching in directory exclusions (#159)
- Handle commit SHA refs in subdirectory package clones (#178)
- Infer `registry_name` when MCP registry API returns empty values (#181)
- Resolve `set()` shadowing and sparse checkout ref issues (#184)
- CLI consistency — align help text with docs (#188)
- `--update` flag now bypasses lockfile SHA to fetch latest content (#192)
- Clean stale MCP servers on install/update/uninstall and prevent `.claude` folder creation (#201)
- Harden plugin security, validation, tests, and docs (#208)
- Use `CREATE_PR_PAT` for agentic workflows in Microsoft org (#144)

### Changed

- Unified `deployed_files` manifest for safe integration lifecycle (#163)
- Exclude `apm_modules` from compilation scanning and cache `Set[Path]` for performance (#157)
- Performance optimization for deep dependency trees (#173)
- Upgrade GitHub Agentic Workflows to v0.52.1 (#141)
- Fix CLI reference drift from consistency reports (#160, #161)
- Replace CHANGELOG link with roadmap discussion in docs index (#196)
- Update documentation for features from 2026-03-07 (#195)

## [0.7.4] - 2025-03-03

### Added

- Support hooks as an agent primitive with install-time integration and dependency display (hooks execute at agent runtime, not during `apm install`) (#97)
- Deploy agents to `.claude/agents/` during `apm install` (#95)
- Promote sub-skills inside packages to top-level `.github/skills/` entries (#102)

### Fixed

- Fix skill integration bugs, transitive dep cleanup, and simplification (#107)
- Fix transitive dependency handling in compile and orphan detection (#111)
- Fix virtual subdirectory deps marked as orphaned, skipping instruction processing (#100)
- Improve multi-host error guidance when `GITHUB_HOST` is set (#113, #130)
- Support spaces in Azure DevOps project names (#92)
- Fix GitHub Actions workflow permissions, integration test skip-propagation, and test corrections (#87, #106, #109)

### Changed

- Migrated to Microsoft OSS organization (#85, #105)
- Added CODEOWNERS, simplified PR/issue templates, triage labels, and updated CONTRIBUTING.md (#115, #118)
- Added missing `version` field in the apm.yml README example (#108)
- Slim PR pipelines to Linux-only, auto-approve integration tests, added agentic workflows for maintenance (#98, #103, #104, #119)


## [0.7.3] - 2025-02-15

### Added

- **SUPPORT.md**: Added Microsoft repo-template support file directing users to GitHub Issues and Discussions for community support

### Changed

- **README Rewording**: Clarified APM as "an open-source, community-driven dependency manager" to set correct expectations under Microsoft GitHub org
- **Microsoft Open Source Compliance**: Updated LICENSE, SECURITY.md, CODE_OF_CONDUCT.md, CONTRIBUTING.md, and added Trademark Notice to README
- **Source Integrity**: Fixed source integrity for all integrators and restructured README

### Fixed

- **Install Script**: Use `grep -o` for single-line JSON extraction in install.sh
- **CI**: Fixed integration test script to handle existing venv from CI workflow

### Security

- Bumped `azure-core` 1.35.1 → 1.38.0, `aiohttp` 3.12.15 → 3.13.3, `pip` 25.2 → 26.0, `urllib3` 2.5.0 → 2.6.3

## [0.7.2] - 2025-01-23

### Added

- **Transitive Dependencies**: Full dependency resolution with `apm.lock` lockfile generation

### Fixed

- **Install Script and `apm update`**: Repaired corrupted header in install.sh. Use awk instead of sed for shell subprocess compatibility. Directed shell output to terminal for password input during update process.

## [0.7.1] - 2025-01-22

### Fixed

- **Collection Extension Handling**: Prevent double `.collection.yml` extension when user specifies full path
- **SKILL.md Parsing**: Parse SKILL.md directly without requiring apm.yml generation
- **Git Host Errors**: Actionable error messages for unsupported Git hosts

## [0.7.0] - 2024-12-19

### Changed

- **Native Skills Support**: Skills now install to `.github/skills/` as the primary target (per [agentskills.io](https://agentskills.io/) standard)
- **Skills ≠ Agents**: Removed skill → agent transformation; skills and agents are now separate primitives
- **Explicit Package Types**: Added `type` field to apm.yml (`instructions`, `skill`, `hybrid`, `prompts`) for routing control
- **Skill Name Validation**: Validates and normalizes skill names per agentskills.io spec (lowercase, hyphens, 1-64 chars)
- **Claude Compatibility**: Skills also copy to `.claude/skills/` when `.claude/` folder exists

### Added

- Auto-creates `.github/` directory on install if neither `.github/` nor `.claude/` exists

## [0.6.3] - 2025-12-09

### Fixed

- **Selective Package Install**: `apm install <package>` now only installs the specified package instead of all packages from apm.yml. Previously, installing a single package would also install unrelated packages. `apm install` (no args) continues to install all packages from the manifest.

## [0.6.2] - 2025-12-09

### Fixed

- **Claude Skills Integration**: Virtual subdirectory packages (like `ComposioHQ/awesome-claude-skills/mcp-builder`) now correctly trigger skill generation. Previously all virtual packages were skipped, but only virtual files and collections should be skipped—subdirectory packages are complete skill packages.

## [0.6.1] - 2025-12-08

### Added

- **SKILL.md as first-class primitive**: meta-description of what an APM Package does for agents to read
- **Claude Skills Installation**: Install Claude Skills directly as APM Packages
- **Bidirectional Format Support**:
  - APM packages → SKILL.md (for Claude target)
  - Claude Skills → .agent.md (for VSCode target)
- **Skills Documentation**: New `docs/skills.md` guide

## [0.6.0] - 2025-12-08

### Added

- **Claude Integration**: First-class support for Claude Code and Claude Desktop
  - `CLAUDE.md` generation alongside `AGENTS.md`
  - `.claude/commands/` auto-integration from installed packages
  - `SKILL.md` generation for Claude Skills format
  - Commands get `-apm` suffix (same pattern as VSCode prompts)

- **Target Auto-Detection**: Smart compilation based on project structure
  - `.github/` only → generates `AGENTS.md` + VSCode integration
  - `.claude/` only → generates `CLAUDE.md` + Claude integration
  - Both folders → generates all formats
  - Neither folder → generates `AGENTS.md` only (universal format)

- **`target` field in apm.yml**: Persistent target configuration
  ```yaml
  target: vscode  # or claude, or all
  ```
  Applies to both `apm compile` and `apm install`

- **`--target` flag**: Override auto-detection
  ```bash
  apm compile --target claude
  apm compile --target vscode
  apm compile --target all
  ```

### Fixed

- Virtual package uninstall sync: `apm uninstall` now correctly removes only the specific virtual package's integrated files (uses `get_unique_key()` for proper path matching)

### Changed

- `apm compile` default: Changed from `--target all` to auto-detect
- README refactored with npm-style zero-friction onboarding
- Documentation reorganized with Claude integration guide

## [0.5.9] - 2025-12-04

### Fixed

- **ADO Package Commands**: `compile`, `prune`, and `deps list` now work correctly with Azure DevOps packages

## [0.5.8] - 2025-12-02

### Fixed

- **ADO Path Structure**: Azure DevOps packages now use correct 3-level paths (`org/project/repo`) throughout install, discovery, update, prune, and uninstall commands
- **Virtual Packages**: ADO collections and individual files install to correct 3-level paths
- **Prune Command**: Fixed undefined variable bug in directory cleanup

## [0.5.7] - 2025-12-01

### Added

- **Azure DevOps Support**: Install packages from Azure DevOps Services and Server
  - New `ADO_APM_PAT` environment variable for ADO authentication (separate from GitHub tokens)
  - Supports `dev.azure.com/org/project/_git/repo` URL format
  - Works alongside GitHub and GitHub Enterprise in mixed-source projects
- **Debug Mode**: Set `APM_DEBUG=1` to see detailed authentication and URL resolution output

### Fixed

- **GitHub Enterprise Private Repos**: Fixed authentication for `git ls-remote` validation on non-github.com hosts
- **Token Selection**: Correct token now used per-platform (GitHub vs ADO) in mixed-source installations

## [0.5.6] - 2025-12-01

### Fixed

- Enterprise GitHub host support: fallback clone now respects `GITHUB_HOST` env var instead of hardcoding github.com
- Version validation crash when YAML parses version as numeric type (e.g., `1.0` vs `"1.0"`)

### Changed

- CI/CD: Updated runner from macos-13 and macos-14 to macos-15 for both x86_64 and ARM64 builds

## [0.5.5] - 2025-11-17

### Added
- **Context Link Resolution**: Automatic markdown link resolution for `.context.md` files across installation and compilation
  - Links in prompts/agents automatically resolve to actual source locations (`apm_modules/` or `.apm/context/`)
  - Works everywhere: IDE, GitHub, all coding agents supporting AGENTS.md
  - No file copying needed—links point directly to source files

## [0.5.4] - 2025-11-17

### Added
- **Agent Integration**: Automatic sync of `.agent.md` files to `.github/agents/` with `-apm` suffix (same pattern as prompt integration)

### Fixed
- `sync_integration` URL normalization bug that caused ALL integrated files to be removed during uninstall instead of only the uninstalled package's files
  - Root cause: Metadata stored full URLs (`https://github.com/owner/repo`) while dependency list used short form (`owner/repo`)
  - Impact: Uninstalling one package would incorrectly remove prompts/agents from ALL other packages
  - Fix: Normalize both URL formats to `owner/repo` before comparison
  - Added comprehensive test coverage for multi-package scenarios
- Uninstall command now correctly removes only `apm_modules/owner/repo/` directory (not `apm_modules/owner/`)

## [0.5.3] - 2025-11-16

### Changed
- **Prompt Naming Pattern**: Migrated from `@` prefix to `-apm` suffix for integrated prompts
- **GitIgnore Pattern**: Updated from `.github/prompts/@*.prompt.md` to `.github/prompts/*-apm.prompt.md`

### Migration Notes
- **Existing Users**: Old `@`-prefixed files will not be automatically removed
- **Action Required**: Manually delete old `@*.prompt.md` files from `.github/prompts/` after upgrading

## [0.5.2] - 2025-11-14

### Added
- **Prompt Integration with GitHub** - Automatically sync downloaded prompts to `.github/prompts/` for GitHub Copilot

### Changed
- Improved installer UX and console output

## [0.5.1] - 2025-11-09

### Added
- Package FQDN support - install from any Git host using fully qualified domain names (thanks @richgo for PR #25)

### Fixed
- **Security**: CWE-20 URL validation vulnerability - proper hostname validation using `urllib.parse` prevents malicious URL bypass attacks
- Package validation HTTPS URL construction for git ls-remote checks
- Virtual package orphan detection in `apm deps list` command

### Changed
- GitHub Enterprise support via `GITHUB_HOST` environment variable (thanks @richgo for PR #25)
- Build pipeline updates for macOS compatibility

## [0.5.0] - 2025-10-30

### Added - Virtual Packages
- **Virtual Package Support**: Install individual files directly from any repository without requiring full APM package structure
  - Individual file packages: `apm install owner/repo/path/to/file.prompt.md`
- **Collection Support**: Install curated collections of primitives from [Awesome Copilot](https://github.com/github/awesome-copilot): `apm install github/awesome-copilot/collections/collection-name`
  - Collection manifest parser for `.collection.yml` format
  - Batch download of collection items into organized `.apm/` structure
  - Integration with github/awesome-copilot collections

### Added - Runnable Prompts
- **Auto-Discovery of Prompts**: Run installed prompts without manual script configuration
  - `apm run <prompt-name>` automatically discovers and executes prompts without having to wire a script in `apm.yml`
  - Search priority: local root → .apm/prompts → .github/prompts → dependencies
  - Qualified path support: `apm run owner/repo/prompt-name` for disambiguation
  - Collision detection with helpful error messages when multiple prompts found
  - Explicit scripts in apm.yml always take precedence over auto-discovery
- **Automatic Runtime Detection**: Detects installed runtime (copilot > codex) and generates proper commands
- **Zero-Configuration Execution**: Install and run prompts immediately without apm.yml scripts section

### Changed
- Enhanced dependency resolution to support virtual package unique keys
- Improved GitHub downloader with virtual file and collection package support
- Extended `DependencyReference.parse()` to detect and validate virtual packages (3+ path segments)
- Script runner now falls back to prompt discovery when script not found in apm.yml

### Developer Experience
- Streamlined workflow: `apm install <file>` → `apm run <name>` works immediately
- No manual script configuration needed for simple use cases
- Power users retain full control via explicit scripts in apm.yml
- Better error messages for ambiguous prompt names with disambiguation guidance

## [0.4.3] - 2025-10-29

### Added
- Auto-bootstrap `apm.yml` when running `apm install <package>` without existing config
- GitHub Enterprise Server and Data Residency Cloud support via `GITHUB_HOST` environment variable
- ARM64 Linux support

### Changed
- Refactored `apm init` to initialize projects minimally without templated prompts and instructions
- Improved next steps formatting in project initialization output

### Fixed
- GitHub token fallback handling for Codex runtime setup
- Environment variable passing to subprocess in smoke tests and runtime setup

## [0.4.2] - 2025-09-25

- Copilot CLI Support

## [0.4.1] - 2025-09-18

### Fixed
- Fix prompt file resolution for dependencies in org/repo directory structure
- APM dependency prompt files now correctly resolve from `apm_modules/org/repo/` paths
- `apm run` commands can now find and execute prompt files from installed dependencies
- Updated unit tests to match org/repo directory structure for dependency resolution

## [0.4.0] - 2025-09-18

- Context Packaging
- Context Dependencies
- Context Compilation
- GitHub MCP Registry integration
- Codex CLI Support
