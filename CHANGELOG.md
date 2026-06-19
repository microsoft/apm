# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `apm audit` now surfaces unmanaged files in governance directories as a single enriched report: each finding states a factual reason (`not tracked in apm.lock.yaml`), a lazy primitive-type tag (`[type: skill|agent|instruction|mcp]`), and a deny-conflict note (`matches deny rule (<pattern>)`) when the path matches the policy's own `dependencies.deny` / `mcp.deny`. A new `unmanaged_files.exclude` policy key suppresses known harness-managed paths, and a symlink guard prevents following links out of the workspace. This is drift / divergence visibility, not supply-chain-attack prevention. (closes #1775) (#1793)
- Azure DevOps is now documented as a first-class marketplace authoring host: a `marketplace.sourceBase` of `https://dev.azure.com/{org}/{project}/_git` composes relative package sources and preserves the `dev.azure.com` host through to the consumer (authenticated with `ADO_APM_PAT`). The end-to-end authoring -> consume path is pinned by a hermetic test. (closes #1010) (#1810)
- `apm install --target antigravity` and `apm compile -t antigravity` add
  Google Antigravity CLI (`agy`, successor to Gemini CLI) as a new target.
  Instructions deploy as rules to `.agents/rules/<name>.md`, skills to
  `.agents/skills/<name>/SKILL.md`, hooks merge into a single
  `.agents/hooks.json` using Antigravity's native schema
  (`PreToolUse`/`PostToolUse`/`PreInvocation`/`PostInvocation`/`Stop`), and
  MCP servers write to a dedicated `.agents/mcp_config.json` (project) or
  `~/.gemini/config/mcp_config.json` (`--global`). `compile` emits `AGENTS.md`.
  Antigravity shares the cross-tool `.agents/` root, so it is explicit-only:
  select it with `--target antigravity` or in `apm.yml` `targets:`; it is
  never auto-detected and is not part of `--target all`. (by @sergio-sisternes-epam;
  closes #1650) (#1770)
- Two additive, default-off policy keys under the existing `security:` namespace: `security.integrity.require_hashes` makes `apm install` fail closed when any non-local lockfile entry lacks a content hash, and `security.audit.fail_on_drift` makes `apm audit` exit non-zero when the workspace drifts from the lockfile. Both only tighten through policy inheritance. (#1794)
- MCP dependencies can now carry harness-specific passthrough keys (for example Claude Code's remote-MCP `oauth` block with `clientId`/`callbackPort`); previously any key outside the modeled set was silently dropped on render. Passthrough keys round-trip into the generated config for every installed harness and cannot shadow a modeled field (`command`/`url`/`headers`/`env`/...), which are rejected with a warning. A future fail-closed tightening to an explicit `extra:` block is tracked in #1806. (closes #1670) (#1765)
- `apm install owner/repo#ref` now routes to the configured default registry (project `registries.default` or `registry.<name>.default true` in `~/.apm/config.json`) instead of probing GitHub. A version selector (`#<ref>`) is required; omitting it exits `1`. Non-semver selectors (`stable`, `main`, a branch name, or any opaque string) are exact-matched against the registry's published version list. Use the `git:` URL form in `apm.yml` to force the GitHub path. (#1816)

### Changed

- Tightened Stage 2 code-complexity thresholds (`max-statements` 120,
  `max-branches` 40, `max-complexity` 35, `max-args` 12, `max-returns` 8) and
  lowered the source file-length guardrail to 800 lines, splitting the remaining
  oversized `policy/` and `marketplace/` modules into focused submodules with no
  behaviour change. (#1681)

### Removed

- `apm marketplace publish` command and consumer-repo fan-out workflow; consumers should run `apm install --update` instead. (#1134)
- `apm marketplace doctor` subcommand alias (deprecated); use `apm doctor` instead. (#1134)

### Fixed

- Registry deps with non-semver version selectors (e.g. `stable`, `main`) no longer report perpetual `outdated`. The drift check now uses literal equality for non-semver registry pins rather than range comparison, which always returned `True` against a semver range. (#1816)
- Non-semver registry version selectors are now exact-matched against the registry's published version list at install time. Previously they were rejected with "not a valid semver range". (#1816)

- Cursor hook integration: emit required top-level `version: 1` in `.cursor/hooks.json`.
  Affected versions: v0.14.1-v0.20.0. Hooks were silently ignored by Cursor on those
  versions. Run `apm install` (or `apm install --target cursor`) to repair existing
  installations. (closes #1823) (#1840)
- `apm update --target` help text now lists `kiro` as a valid example
  target, matching `apm install`. (#1821)
- `apm marketplace check` no longer fails with exit 128 for entries on
  non-default hosts, including relative entries composed onto
  `marketplace.sourceBase` (self-managed GitLab / GHES / Azure DevOps). It now
  resolves each entry against its effective host and per-org token, matching
  `apm pack`; local `./` packages skip the network.
  (closes #1762, follows up #1736)
- Policy inheritance no longer drops `fetch_failure`, `registry_source`, and `bin_deploy` when a policy `extends` another; these fields now carry and tighten through the merge like sibling sections. (closes #1778) (#1791)
- `apm install` no longer silently ignores MCP servers declared in `devDependencies.mcp`; dev MCP configs and lockfile entries now stay in sync on fresh installs. (closes #1780) (#1787)
- `apm compile` now honors `managed_section` mode on distributed root
  `AGENTS.md` and `--single-agents` writes, preserving hand-authored
  content outside the APM markers. (closes #1764) (#1768)
- `apm install` now removes orphaned skill directories when a package is uninstalled or its skills are renamed. Previously, individual files were deleted but the skill folder remained with a "Refused to remove directory entry" warning. (closes #1483) (#1767)

## [0.20.0] - 2026-06-11

### Added

- `apm install --target kiro` deploys Kiro IDE steering, skills, hooks,
  and MCP config to the documented `.kiro/` layout. (by @TibRib; closes #702) (#1741)
- APM now catches accidental subpath embeds in git URLs (for example, `org/repo/skills/name.git`) and points at the supported `path:` key form. (#1740)
- SHA-pinned dependencies now stay current automatically: `apm update` resolves the newest annotated release tag, rewrites the pin, and `apm outdated` reports drift. (#1738)
- `apm install --target hermes` and `apm compile -t hermes` add the Hermes
  agent (Nous Research) as a new experimental target (opt in via
  `apm experimental enable hermes`). Skills deploy to `.agents/skills/<name>/SKILL.md`
  (project) or `~/.hermes/skills/<name>/SKILL.md` (`--global`), `compile`
  emits `AGENTS.md`, and MCP servers are written to the `mcp_servers:` block
  of `~/.hermes/config.yaml` (written atomically with `0o600` perms, preserving
  unrelated config keys and refusing to overwrite a malformed file). `HERMES_HOME`
  overrides the Hermes home directory. See the [Hermes integration guide](https://microsoft.github.io/apm/integrations/hermes/). (#1726)
- Enterprise marketplace authors can stop repeating shared git base paths by
  setting `sourceBase` (one line replaces repeated URLs), while host-prefixed,
  full-URL, and local entries remain per-entry overrides. (#1736)
- `apm marketplace add` now accepts git URLs with `#ref`, local file paths,
  and hosted `marketplace.json` URLs -- so teams can consume private,
  offline, or hosted catalogs without publishing a GitHub repo (closes
  #676). (#1739)
- Enterprise bootstrap mirror mode (`APM_RELEASE_METADATA_URL`, `APM_RELEASE_BASE_URL`, `APM_INSTALLER_BASE_URL`, `APM_PYPI_INDEX_URL`, `APM_NO_DIRECT_FALLBACK`) routes `install.sh`, `install.ps1`, and `apm self-update` through internal mirrors with fail-closed public fallback; closes #1680. (#1733)
- `apm pack --archive-format [zip|tar.gz]` escape hatch (default `zip`) lets
  CI pipelines that depended on the previous `.tar.gz` default opt back in without
  changing the project default. Passing `--archive-format` without `--archive` is
  now a `UsageError`. (#1720)

### Changed

- **BREAKING:** `apm pack --archive` now produces `.zip` by default instead of `.tar.gz`, matching the format produced by `apm publish` and expected by Claude Code and plugin hosts while staying natively extractable on Windows without WSL or a tar binary. Note: ZIP archives are typically 30-130% larger than `.tar.gz` for text-heavy skill bundles due to per-file compression; use `--archive-format tar.gz` if archive size is a priority. (#1720)

### Fixed

- `apm pack` now fills local-path marketplace package `description` and `version` from each package's own `apm.yml` when the root `marketplace.packages[]` entry omits them, so monorepo marketplaces no longer show empty browse columns -- matching the existing remote-source fallback. (closes #1725) (#1755)
- `apm install --target` with a comma-separated list containing `copilot` (or the `vscode`/`agents` aliases) no longer silently drops the Copilot target. (by @Addono, closes #1746, #1749)
- `apm install` now resolves relative `path:` deps declared by remote monorepo packages when they stay inside the same remote repo, while still rejecting absolute, escaping, or cross-repo paths; closes #1571. (#1732)
- Optional-auth MCP registry servers now install without token prompts when values are unset; `apm install` also omits empty runtime config entries and preserves user-edited optional values on reinstall; refs #20. (#1734)
- Dependencies with the same path on different git hosts no longer collide in `apm.lock.yaml`; `apm install` keeps GitHub/GitLab PATs off generic-host file downloads, routes bespoke GitLab hosts through `type: gitlab`, and surfaces non-404 download failures with host and endpoint context. Reading private files from a generic non-default host now requires a whole-repo git dependency or explicit backend signal; see the dependency and lockfile docs (closes #773). (#1735)
- GitLab `path:` files now just work on self-hosted instances where the REST API is disabled or restricted -- APM fetches via git transport automatically. `GITLAB_APM_PAT` / `GITLAB_TOKEN` remain available as a thin REST API fallback; closes #1014. (#1740)
- `apm compile --clean --target claude` now removes a stale APM-generated
  `CLAUDE.md` when instructions have already been deployed to `.claude/rules/`
  (the dedup path introduced in #1138), so the duplicate-context problem #1138
  set out to eliminate is fully resolved even for projects that compiled before
  `.claude/rules/` existed. Hand-authored `CLAUDE.md` files (those lacking the
  `<!-- Generated by APM CLI -->` marker) are never deleted; a warning is
  emitted instead. Plain `apm compile` (without `--clean`) remains
  non-destructive. (closes #1729, follow-up to #1138)
- `apm compile --target copilot` no longer writes empty `AGENTS.md`
  shell files when `.github/instructions/` already holds deployed
  instructions; `--clean` also removes stale APM-generated shells while
  preserving hand-authored files. Use `--no-dedup` / `--force-instructions`
  to write full `AGENTS.md` files anyway. (by @tillig; closes #1730, related:
  #1138, #1550) (#1742)

## [0.19.0] - 2026-06-09

### Added

- `apm install <package> --target openclaw` adds OpenClaw as a new experimental
  skills consumer target (opt in via `apm experimental enable openclaw`). Skills
  deploy to `.agents/skills/<name>/SKILL.md` (project) or
  `~/.openclaw/skills/<name>/SKILL.md` (global). (by @sergio-sisternes-epam, #1677)
- `apm publish` auto-pack now includes `README.md`, `CHANGELOG.md`, and `LICENSE` / `LICENCE` (case-insensitive, symlinks excluded) in the flat registry archive, matching npm's behaviour of bundling standard root-level documentation files alongside the package source. (by @nadav-y, #1695)
- `apm install` now surfaces per-event hook action summaries as it integrates
  hook primitives, with the fully rewritten hook JSON shown under `--verbose`,
  so you can audit exactly what each hook runs at install time. The summary is
  built from the post-rewrite data actually written to disk and executed -- it
  faithfully reflects on-disk content across Copilot, Claude, and Gemini
  targets (including OS-specific `windows`/`linux`/`osx` hook keys). (by
  @harshitlarl, closes #316, #1700)
- Experimental, Copilot-only `canvas` primitive: `.apm/extensions/<name>/extension.mjs` deploys to `.github/extensions/<name>/` on `apm install`/`apm pack`, gated by `apm experimental enable canvas` + `--trust-canvas-extensions` for dependencies. (by @sergio-sisternes-epam, #1689)
- Experimental canvas: `apm install --global --trust-canvas-extensions` deploys dependency canvases to `~/.copilot/extensions/<name>/`; `apm uninstall --global` prunes them. (by @sergio-sisternes-epam, #1689)
- Experimental canvas: package validation recognises canvas extensions (canvas-only packages valid, gated-executable warning); `apm audit` (text) lists deployed canvas bundles. (by @sergio-sisternes-epam, #1689)

### Changed

- **BREAKING:** `apm publish` auto-pack now produces a `.zip` archive (ZIP\_DEFLATED) instead of `.tar.gz`, aligning with the de facto standard used by Anthropic Claude, OpenAI, and Google Gemini agent platforms. The `--tarball` option is renamed to `--zip`. Output filename: `{name}-{version}.zip`. (by @nadav-y, #1695)
- `MCPDependency.from_dict()` now emits a `[!]` warning naming every unknown
  key dropped during parsing (e.g. harness-specific fields like `oauth`) instead
  of discarding them silently, making forward-compat config mismatches
  diagnosable. (#1674)
- Auth credential cascade now emits debug-level logs for every fallback step,
  making token misconfiguration diagnosable without adding noise to normal output.
  Enable with ``apm --verbose`` or ``APM_LOG_LEVEL=DEBUG``.
  (by @danielmeppiel, closes #935, #1664)

### Fixed

- `apm audit` now detects drift and tampering in committed deployed skill
  bundles under `.agents/skills/**`. The lockfile integrity manifest is now
  target-complete (per-file `deployed_files` + `deployed_file_hashes` for every
  committed file-based deploy target, not just one), the drift differ walks each
  primitive's `deploy_root`, and the `apm-self-check` CI job runs the drift gate
  (dropped `--no-drift`). Previously a multi-target deploy left the manifest
  single-target, so deployed skill content could silently diverge from its
  `packages/**` / `.apm/**` source with every gate staying green. (by
  @danielmeppiel, closes #1716, #1718)
- `apm install` now falls back to an AAD bearer token (via `az login`) when no
  `ADO_APM_PAT` is configured for Azure DevOps file downloads, and fail-closes
  when ADO returns an interactive HTML sign-in page with HTTP 200 instead of
  writing corrupt HTML to disk. (by @danielmeppiel, closes #1671, #1675)
- `apm install` now splits FQDN monorepo subpath shorthand on GitHub
  Enterprise Server hosts. With `GITHUB_HOST` set, a dependency string like
  `ghe.example.com/org/repo/packages/skill` resolves to `git: org/repo` plus
  `path: packages/skill` instead of embedding the whole subpath into the clone
  URL. (by @sergio-sisternes-epam, closes #1673, #1684)
- `apm compile` now keeps instruction content in `AGENTS.md` for non-Copilot
  targets (Codex, OpenCode, Windsurf) instead of suppressing it whenever
  `.github/instructions/` contains `.md` files, so those targets no longer lose
  their instructions. (by @sergio-sisternes-epam, closes #1678, #1685)
- `apm install` now unwraps the `{ "lspServers": { ... } }` envelope in plugin
  `.lsp.json` files instead of silently skipping them with a misleading
  validation error. (by @sergio-sisternes-epam, closes #1683, #1686)
- `apm install` now preserves transitive dependencies declared in `apm.yml`
  when installing dual-format packages (those colocating `plugin.json` with
  `apm.yml`) from the marketplace or a remote subdirectory, instead of silently
  dropping them when the synthesized manifest overwrote the file. A malformed
  existing `apm.yml` now also surfaces a warning instead of failing silently.
  (by @sergio-sisternes-epam, closes #1666, #1687)
- `apm audit` external skill-scanner UX is clearer, with improved messaging and
  error handling around external scanners. (by @sergio-sisternes-epam, #1692)
- `install.sh` now validates `APM_LIB_DIR` before running `rm -rf`, preventing
  data loss when the override points at a directory holding unrelated
  application data. (by @dohwi, closes #1690, #1694)
- `apm install` now preserves scoped MCP package config keys such as
  `@playwright/mcp` across Claude, Codex, and Copilot harness configs instead
  of truncating them to `mcp`. (#1699)
- `apm install` now accepts marketplace git-subdir refs with package-version
  tags such as `package@v1.0.1` inside the `#ref` fragment while still
  rejecting retired string-form `@alias` shorthand. (closes #1696, #1698)
- `apm install` now keeps format-transformed rule files (`.claude/rules`,
  `.cursor/rules`, `.windsurf/rules`) tracked in `managed_files` and rewrites
  them when the source instruction changes, instead of mis-classifying them as
  user-authored collisions and skipping them; also fixes a mislabeled
  `windsurf_rules` entry in install output. (by @srid, closes #1662, #1665)
- `apm install <local-path>` now dereferences in-package symlinks into regular
  files so that local and remote installs produce consistent output. Symlinks
  whose resolved target escapes the package root hard-fail with a
  `PathTraversalError`; circular directory-symlink chains and unreadable
  package directories are detected deterministically. Previously, in-package
  symlinks were silently dropped by the downstream deploy filter. (by
  @danielmeppiel, closes #1668, #1676)
- `apm install --skill <name>` is now honored for non-package skill-collection
  installs (plugin-manifest collections), promoting only the selected skills
  instead of the entire set; both leaf names and nested manifest paths are
  accepted. (by @danielmeppiel, closes #1707, #1709)
- `apm install` no longer rewrites `apm.lock.yaml` when a project combines a
  remote APM dependency with unchanged local `.apm/instructions` content.
  (by @danielmeppiel, closes #1702, #1710)

## [0.18.0] - 2026-06-04

### Added

- `apm marketplace audit`: new command that flags dependencies bypassing
  marketplace pinning, so you can catch unpinned or non-marketplace deps before
  they ship. (by @edenfunf, #881)
- Marketplace dependency object entries in `apm.yml` and `plugin.json` now
  resolve to concrete git refs during install, support optional semver-style
  `version` constraints, and fail closed when resolution errors occur. (by
  @stbenjam, #1422)
- LSP server management: declare language servers in `apm.yml` and `apm install`
  wires them into Claude Code and GitHub Copilot CLI automatically. (by
  @stbenjam, #1424)
- `apm outdated` now recognizes monorepo `{name}-v{version}` tag naming, so
  per-package version checks work in repositories that publish multiple packages
  under subpath tags. (by @kevinbeier-enbw, #1504)

### Changed

- **BREAKING:** `apm` now rejects string-form `@alias` dependency shorthand at
  parse time with a migration error; use the object form with `alias:` instead.
  (#1655)

### Fixed

- `apm compile` now matches `applyTo` globs without following symlinks into
  `node_modules`, avoiding spurious matches and slow traversals in projects with
  symlinked dependencies. (by @srid, #1576)
- `apm install` now rewrites outbound relative links inside skill bundles so the
  links keep resolving after a skill is materialized into an agent target.
  (closes #1625, #1657)
- Git-source semver ranges for virtual subdirectory dependencies now match
  per-package `{name}-v{version}` tags, derive `{name}` from the subpath, and
  reject malformed range-like refs with an actionable manifest error. (closes
  #1633, #1658)
- `apm install` now preserves env-var placeholders (e.g. `${VAR}`) when writing
  IntelliJ `mcp.json`, so configured environment references survive install
  instead of being expanded or dropped. (closes #1656, #1659)

## [0.17.0] - 2026-06-03

### Added

- `apm lock`: new command that resolves all dependencies and writes
  `apm.lock.yaml` **without** deploying any files to agent targets. Mirrors
  the lockfile-only ergonomics of `cargo generate-lockfile` and `pnpm lock`;
  accepts `--update`, `--verbose`, `--global`, `--no-policy`, `--target`, and
  `--parallel-downloads`. (#1639)
- `apm find <file>`: trace a materialized file back to its contributing
  package(s) via a reverse index over `apm.lock.yaml`. Supports `--source` and
  `--path`; untracked paths exit 1 (a missing or corrupt lockfile exits 2),
  each with an ASCII `[x]` message, and the command performs zero network or
  write operations. (#1631)
- `apm update` now accepts `-g/--global`, positional `[PACKAGES]...`,
  `--force`, and `--parallel-downloads`, making it a strict superset of
  `apm deps update` -- one verb covering project and user scope, per-package
  refresh, and collision overwrite. (closes #1525, #1574)
- `apm config set mcp-registry-url <url>` / `get` / `unset` persistently
  configures a private MCP registry endpoint in `~/.apm/config.json`, sitting
  between the `MCP_REGISTRY_URL` env var and the built-in default in the
  resolution chain (CLI flag > env > config > default). Accepts `http://` or
  `https://` only; `apm mcp search` prints a `Registry (config): <url>`
  diagnostic when active. (closes #818, #1637)
- `apm init` now suggests creating an `agentrc` in its Next Steps output when no
  instructions exist, guiding new projects toward a first primitive. (#1611)
- `apm self-update` and the version checker now respect air-gapped env vars
  (`GITHUB_URL`, `APM_REPO`, `VERSION`), so self-update and update checks work
  behind a mirror without reaching public GitHub. (#1615)
- Target-aware hook event diagnostics: `apm install` now reports hook wiring
  per target so misconfigured or unsupported hook events surface clearly
  instead of failing silently. (#1635)
- JetBrains users (IntelliJ IDEA, PyCharm, GoLand, WebStorm, and other IDEs)
  can now install MCP servers with `apm install --mcp --runtime intellij
  <package>` -- no manual `mcp.json` editing required. Auto-detected when the
  OS-specific Copilot-for-JetBrains config directory exists; runtime `${VAR}`
  env substitution is not supported for this target, so `--env` values are
  written verbatim (avoid plaintext secrets). (#1636)
- `apm install` and `apm compile` now accept `--root DIR` to redirect every
  generated artifact (`apm_modules/`, `apm.lock.yaml`, `.gitignore`, and
  harness/`AGENTS.md`/per-target files) under `DIR`, while `apm.yml`, `.apm/`,
  and local-path dependencies still resolve from `$PWD` -- mirroring
  `pip install --target` and `npm install --prefix`. `--root` is rejected with
  `--global` (install) and `--watch` (compile). Thanks to @srid (juspay) for
  the original implementation. (closes #888, #1628)
- `apm pack` now generates an ecosystem-specific `plugin.json` when
  `target:`/`targets:` includes `claude` or `copilot`, synthesised from
  `apm.yml` identity fields so one source tree drops into a Claude Code or
  Copilot plugin path with zero hand-editing. Claude manifests embed
  `mcpServers` from `.mcp.json` with credential-bearing keys and secret-shaped
  values stripped recursively; Copilot manifests omit `mcpServers`. An existing
  `plugin.json` is preserved unless `--force` is passed. (#1623)
- `apm pack` also synthesises `homepage`, `repository`, `keywords`, and a
  structured `author` (`{name, email?, url?}`) from `apm.yml` into
  `plugin.json`. All fields are additive and `author` still accepts a plain
  string (backward-compatible). (closes #1621, #1624)
- `apm install -g` now deploys `bin/` executables from `marketplace_plugin`
  packages into `~/.claude/skills/<name>/bin/` and makes them executable,
  giving Claude Code direct access to plugin-provided binaries. The
  `bin_deploy` policy field lets administrators opt out globally
  (`deny_all: true`) or per-package. (#1591)
- `apm compile` with `compilation.agents_md.mode: managed_section` updates only
  the APM-owned block between configurable markers, so teams with existing
  `AGENTS.md` content can adopt `apm compile` without losing hand-written
  rules. Missing or duplicate markers raise a loud error so no content is
  silently lost. (closes #1540, #1589)
- `apm compile --no-dedup` (alias `--force-instructions`) forces the
  instructions section into `CLAUDE.md` even when `.claude/rules/` is already
  populated. Claude target only; Copilot deduplication is always on. (closes
  #1463, #1616)
- `apm publish` auto-pack now includes `README.md`, `CHANGELOG.md`, and
  `LICENSE`/`LICENCE` (case-insensitive, symlinks excluded) in the flat
  registry archive, matching npm's bundling of standard root-level docs.
  (#1562)
- `install.sh` and `apm self-update` now send a conditional `Authorization`
  header on GitHub release-lookup API calls when `GITHUB_APM_PAT`,
  `GITHUB_TOKEN`, or `GH_TOKEN` is set, improving reliability on shared IPs and
  corporate NAT. Anonymous fallback is preserved when no token is configured.
  (closes #1582, #1588)
- **Experimental (`external-scanners`):** `apm audit` can now ingest findings
  from external SARIF 2.1.0 scanners (e.g. NVIDIA SkillSpector) via `--external
  <name>` and `--external-sarif <file>`, merging them into APM's own report and
  exit codes. APM's native content scan always runs; external findings are
  purely additive. Enable with `apm experimental enable external-scanners`.
  (#1579)
- **Experimental (`external-scanners`):** `apm install` can run an optional
  content audit over freshly deployed files (native hidden-character scan plus
  any policy-required external scanners). Off by default; opt in with
  `apm install --audit warn|block` (or `--no-audit`), set a default via
  `apm config set audit-on-install`, or mandate it org-wide via `apm-policy.yml`
  `security.audit.on_install`. Policy is a floor: it can raise the mode but
  never relax an org `block` (`--no-policy` opts out per-invocation), and a
  policy-required scanner missing at install time fails closed. (#1579)
- **Experimental (`external-scanners`):** `apm audit` exposes a configuration
  surface for external scanners -- `--external-llm/--no-external-llm` toggles a
  scanner's LLM analysis and `--external-args TEXT` passes shlex-split extra
  flags validated against a per-adapter allowlist (non-allowlisted,
  secret-looking, or out-of-cwd args are rejected fail-closed). Persist
  defaults with `apm config set external.<name>.{llm,args}`; orgs can restrict
  via `security.audit.scanners.<name>.allow_args: false`. LLM mode prints an
  egress banner, forwards `OPENAI_API_KEY`/`NVIDIA_INFERENCE_KEY` only for that
  run, and secret-redacts scanner stderr. (#1647)

### Changed

- `apm compile` no longer emits cosmetic debug comments (APM version,
  source-file headers, footer) in generated `CLAUDE.md` and
  `copilot-instructions.md` by default: `compilation.source_attribution` now
  defaults to `false` (was `true`), cutting token overhead in every LLM context
  that reads these files. Load-bearing markers are always emitted; set
  `compilation: source_attribution: true` to restore. (closes #1341, #1584)
- `apm pack` bundle export now strips credential-bearing keys and secret-shaped
  values from `.mcp.json` recursively before writing the bundle's merged
  `.mcp.json`. If consumers rely on those keys, replace them with `$ENV_VAR`
  references and inject values at MCP-host startup. (#1623)

### Deprecated

- `apm deps update` is deprecated in favor of `apm update`, which now exposes
  every flag it had. It prints a one-line banner and keeps working for one
  release; it will be removed in the next breaking release. (closes #1525,
  #1574)

### Removed

- **BREAKING:** `apm pack --marketplace-output PATH` has been removed.
  Deprecated in v0.14 with a stderr warning and auto-translated to
  `--marketplace-path claude=PATH`; use `--marketplace-path claude=PATH` to
  override the Claude output path. (closes #1318, #1585)

### Fixed

- `apm pack --check-clean` now emits a copy-pasteable recovery recipe
  (`git commit --amend --no-edit` + `git push --force-with-lease`, or a
  follow-up commit variant) when `marketplace.json` drifts from source, so
  producers get the right command at the point of failure. (closes #1381,
  #1610)
- `apm mcp install --help` epilog now references `apm install --help` instead
  of the invalid `apm install --mcp --help` combination that always raised a
  UsageError. (closes #1586, #1604)
- Custom-port credential errors now include a ready-to-run `git credential fill`
  verification command and a link to the auth troubleshooting docs. (closes
  #799, #1608)
- `apm install` now shows a recovery hint (`apm install --no-policy`) when the
  `required-packages-deployed` policy check fails, so users know how to unblock.
  (closes #1314, #1617)
- `apm install -g` now deploys `instructions` primitives for the Copilot target
  by concatenating each package's `*.instructions.md` into
  `~/.copilot/copilot-instructions.md` (the single file Copilot CLI reads at
  user scope); previously this primitive type was silently skipped for global
  installs. Each contribution is wrapped in an HTML provenance comment for
  auditability. (closes #650, #1619)
- `apm compile` with `managed_section` mode now raises a clear error when the
  target file does not exist instead of a confusing "markers not found"; create
  the file with markers first, or use `mode: full` for the initial generation.
  (closes #1593, #1606)
- `apm compile --target copilot` (and `agents`) no longer writes instructions
  into `AGENTS.md` when `apm install` already deployed them to
  `.github/instructions/`, eliminating duplicate context Copilot would read from
  both locations. Mirrors the existing Claude-path dedup. (closes #1550, #1590)
- `apm install` skips the `.gitignore` update for global-scope installs, so
  user-scope installs no longer touch a project `.gitignore`. (#1587)
- The primitive parser now normalizes list-valued `applyTo` and parses
  `handoffs` in agent frontmatter, so multi-glob `applyTo` and agent handoff
  declarations are honored instead of silently dropped. (#1629)
- `apm compile` managed-section error messages were polished for clarity when
  markers are missing or malformed. (#1609)
- `apm install` now always writes `apm.lock.yaml` when resolution succeeds,
  closing a gap where the lockfile could be skipped in some local-dependency
  flows. (#1652)
- **Experimental (`external-scanners`):** external SARIF ingestion now strips
  ANSI escape codes from scanner message text and passes `--no-llm` to
  SkillSpector by default while tolerating non-JSON stdout, hardening the
  experimental scanner path. (#1644, #1645)

### Security

- `apm install -g` now deploys `marketplace_plugin` `bin/` executables with
  user-only execute (owner +x; group and other execute bits cleared).
  Previously-deployed files are hardened in place on reinstall, so upgrading APM
  tightens permissions left by older versions. (#1626)

### Performance

- `apm install --update` no longer re-downloads a dependency when the in-flight
  resolution callback already fetched it at the correct SHA, eliminating a
  redundant network round-trip per up-to-date dependency. (closes #551, #1612)

## [0.16.1] - 2026-06-01

### Added

- `apm doctor` is now a top-level command (promoted from `marketplace doctor`) that diagnoses environment problems such as runtime setup, PATH wiring, and configuration, so users can health-check an install without remembering the `marketplace` namespace. (#1539)

### Fixed

- `apm install -g` (USER scope) no longer writes `apm_modules/` to the working-directory `.gitignore`; only project-scope installs update it. (closes #1577)
- `apm install -g --target copilot` now deploys prompt primitives to `~/.copilot/prompts/` while continuing to filter unsupported user-scope instructions. (closes #1482, #1570)
- Linux standalone `apm` binaries no longer fail git shared-cache clones with shared-library symbol lookup errors caused by PyInstaller dynamic-library paths leaking into child processes. (closes #1534)
- Avoid 13-minute `apm install` hangs in large local projects by limiting synthetic `_local` discovery to `.apm/` and `.github/`, while preserving package metadata discovery. (closes #1507) -- by @ioannispoulios
- `apm install -g <package>#<ref>` now updates an existing unpinned global dependency entry instead of leaving the manifest floating. (#1559)
- `apm install` no longer rewrites `apm.lock.yaml` when MCP dependencies are unchanged, keeping no-op installs byte-stable. (#1568)
- `apm install` now honors manifest `targets:` without falling back to the legacy Copilot target when singular `target:` is absent. (#1560)
- `apm install` now preserves executable permission bits when materializing package files through the reflink copy fast path and Artifactory ZIP extraction, so installed executables stay runnable. (closes #1563, #1566)
- `apm install` summary now reflects actual file mutations: a re-install with identical deployed files reports `No changes -- install state already up to date` instead of falsely claiming `Installed 1 APM dependency`. (closes #1557, #1569)
- `apm install --update <pkg>` no longer falls through to all manifest dependencies when the positional request already validates as present; lockfile serialization also de-duplicates `deployed_files` paths. (closes #1558, #1567)
- `apm install` no longer fails on a second run with a false-positive `Content hash mismatch ... supply-chain attack` error for unpinned git or virtual-file dependencies; the redundant re-download is skipped when the on-disk hash matches the lockfile. (closes #1548, #1553)
- `apm uninstall <pkg>` now scans `devDependencies.apm`, so packages added with `apm install --dev` can be removed instead of leaking in `apm.yml`. (closes #1549, #1552)

### Performance

- `apm install` is substantially faster on large projects and monorepos: primitive discovery is memoized across (integrator, target) pairs, the per-file `Path.resolve()` call is dropped, and skipped directories are expanded (up to 30-40x faster in the worst cases). (closes #1533, #1538)

### Documentation

- Surface the `compilation.strategy: distributed` default in the
  concepts ramp and `apm compile` reference so users understand why
  `apm compile` may add `AGENTS.md` / `CLAUDE.md` files in
  subdirectories driven by `applyTo:` scopes. Adds a new "Where
  compiled context files land" section to the primitives-and-targets
  page and a distributed-layout example in the compile reference.
  Default behavior is unchanged. (closes #1447) -- by @tillig

## [0.16.0] - 2026-05-28

### Added

- OpenAPM v0.1 normative spec at `docs/src/content/docs/specs/openapm-v0.1.md` with JSON Schemas for manifest/lockfile/policy and conformance fixtures under `tests/fixtures/spec-conformance/`, so third-party integrators can build conformant tools without reading the rest of the APM docs. (closes #1502, #1517)
- `ref:` on git-source dependencies now accepts semver ranges (`^1.2.0`, `~1.4`, `>=2.0 <3`, `1.5.x`). `apm install` resolves the highest matching remote tag and pins the resolved tag, SHA, and constraint in `apm.lock.yaml`; subsequent installs replay offline, `apm install --update` re-resolves. (closes #1488, #1496)
- `apm deps why <package>` explains why a transitive dependency was installed by walking the lockfile back to your direct declaration in `apm.yml`. Supports `--global` and `--json`; analogue of `npm why` / `yarn why`. (#1495)
- `policy.dependencies.require_pinned_constraint: true` bans unbounded version ranges across `apm.yml`, blocking accidental drift in governed environments. (#1494)
- Deterministic Artifactory boundary probe at install time, so nested GitLab group/subgroup repos behind a JFrog proxy install correctly via the bare-shorthand form (previously failed with HTTP 404). Auth errors (401/403) are now distinguishable from missing-repo errors, and bearer tokens cannot leak cross-host via redirect. (#1472)

### Changed

- **BREAKING:** `apm install` now exits `1` whenever the diagnostic summary reports `Installation failed with N error(s)`. Previously it exited `0` even after reporting errors, so CI could not detect failure. `--force` continues to bypass only the security scan's critical-finding block; it does not suppress general install errors (matches `npm` / `pip` / `cargo`). Callers that asserted `exit_code == 0` while errors were reported must update. (#1496)
- Artifactory parse-time boundary detection no longer relies on directory-name heuristics (`skills/`, `prompts/`, `agents/`, `collections/`, `instructions/`); the install-time HEAD probe is now authoritative for the `(owner, repo, virtual_path)` split. Explicit FQDN refs use a shallow `owner/repo` split; bare shorthand under `PROXY_REGISTRY_ONLY` uses a structural file-extension rule. (#1472)

### Fixed

- `install.ps1` on Windows now works on accounts whose profile path contains non-ASCII characters (e.g. an accented username): the generated `apm.cmd` shim embeds the literal `%LOCALAPPDATA%` token instead of a baked, code-page-mangled path. Previously `apm --version` reported `The system cannot find the path specified.` (closes #1509, #1512)
- `apm.cmd` Windows shim is now written as ASCII so `cmd.exe` parses it correctly when invoked via `PATH`. A previous attempt to write the shim as UTF-16LE broke fresh installs with garbled output and exit code 1. (#1522)
- `install.ps1` is now strict ASCII, so `apm self-update` on `cp1252` Windows consoles no longer crashes with `UnicodeEncodeError: 'charmap' codec can't encode characters`. (#1523)
- `apm install --target opencode` now warns at install time when an agent's frontmatter has shapes OpenCode's Zod schema rejects (`tools:` as list/string, non-hex/non-theme `color:`), so users learn why OpenCode refuses the agent instead of hitting only a runtime crash. (Phase 1 of #581, #1513)
- `apm compile --target claude` now honors `compilation.strategy: single-file` and `--single-agents`, collapsing into one root `CLAUDE.md` instead of silently emitting per-subdirectory files. (closes #1445, #1514)
- `apm install git@gitlab.com:owner/repo.git#ref` now succeeds for users with an SSH key and no `GITLAB_APM_PAT` / `GITLAB_TOKEN`: the validator honors the explicit SSH transport instead of demanding an HTTPS-token probe. GitLab SSH-key users get parity with GitHub SSH users. (closes #1501, #1515)
- `apm install -g` now correctly integrates hook JSON files authored in the "naked" Claude settings-slice format (event names at top-level, no outer `hooks:` wrap). Previously the file parsed cleanly but merged nothing while the summary still reported `1 hook(s) integrated`; the counter now reflects actual contributions and malformed shapes fail closed with a warning. (closes #1499, #1516)
- `apm install --update` now re-resolves direct git-source semver dependencies even when the dependency's install path already exists; previously the BFS resolver short-circuited and `--update` was a silent no-op for git-semver refs. (#1496)
- `policy.dependencies.require_pinned_constraint: true` no longer misclassifies the npm/cargo explicit-equality form `=1.2.3` as `BARE_BRANCH`. Both `1.2.3` and `=1.2.3` are pinned; pip-style `==1.2.3` is still rejected. (follow-up to #1494, #1506)
- `apm uninstall` now fully cleans `.windsurf/skills/<pkg>/` directories on the `windsurf` target. (by @yoelabril, #1486)
- `apm unpack` deprecation banner softened from "will be removed in v0.14" to "will be removed in a future release"; the previous wording shipped past v0.14.0 and contradicted reality. (#1511)

## [0.15.0] - 2026-05-27

### Security

- **BREAKING:** `apm install` against a `*.ghe.com` marketplace now refuses bare cross-repo `repo:` fields in `marketplace.json` before any network request runs, closing a dependency-confusion attack vector where `repo: owner/repo` could silently resolve to an attacker-controlled namespace on public `github.com`. Qualify with `corp.ghe.com/owner/repo` for same-host enterprise deps or `github.com/owner/repo` for declared cross-host deps. (closes #1326) -- by @edenfunf (#1459)

### Added

- `apm config set prefer-ssh true` / `apm config set allow-protocol-fallback true` persist transport preferences to `~/.apm/config.json` so SSH-only and corporate GHES users stop re-passing `--ssh` / `--allow-protocol-fallback` on every `apm install`. Resolution order: CLI flag > env var > `apm config` > default. (closes #1243, #1308)
- Experimental package registry resolver and `apm-policy.yml` source-mandate enforcement -- controlled sources, locked versions, byte-level SHA-256 verification. Git-based dependencies unchanged. Enable with `apm experimental enable registries`; configure per-registry credentials via `apm config set registry.<name>.{url,token}`; mandate via `registry_source: {require: [...], allow_non_registry: false}`. Package signing, SBOM, and SLSA provenance are out of scope for v1. (#1471)
- `marketplace.packages[].source` in `apm.yml` accepts non-default git hosts via the `host.tld/owner/repo` shorthand or the full `https://host.tld/owner/repo[.git]` URL for `apm pack`, unlocking GitHub Enterprise and self-hosted GitLab as first-class marketplace package sources. (#1288)
- `apm marketplace add` now accepts local filesystem paths, `file://` URIs, SSH URLs, and HTTPS URLs to any git host (Azure DevOps via `ADO_APM_PAT`, GitLab, Gitea, Bitbucket Server, self-hosted). Generic-git registrations fetch `marketplace.json` via `GitCache` and never forward APM tokens; local marketplaces read the manifest directly. Same change hardens `GitCache` against malicious upstreams: every clone/fetch/checkout sets `core.hooksPath=/dev/null` and skips submodule recursion. (#1476)

### Changed

- `apm compile` internals: deduplicated compilation config and registry-cursor refactor; no user-visible behaviour change but reduces drift between paths. (#1367)
- `triage-panel` skill: replace invalid `difc:` block with `tools.github.min-integrity` so the gh-aw workflow lints clean and integrity gating actually applies. (#1487)
- `triage-panel` skill: DIFC read-integrity exemption for external issues so triage can read community-reported issue bodies without the gate blocking. (#1462)
- CodeQL integration tests now assert on parsed URL components instead of substrings, removing a class of false-positive matches and matching the repo test convention. (#1492)

### Performance

- Scaling guards on variant-key and cache-lookup paths, wired into the CI integration gate so per-dep resolution regressions fail PRs instead of slipping to main. (#1439)

### Fixed

- `apm install` re-resolves a dependency when its ref pin changes in `apm.yml` and `--refresh` actually re-resolves all pins (previously accepted but no-op) -- by @sergio-sisternes-epam (#1473)
- Copilot, Codex, Cursor, Claude, Windsurf, OpenCode, and Gemini adapters handle MCP v0.1 `runtimeArguments`/`packageArguments` with `variables` (no `type` key), matching the VS Code fix from #1444. (#1461, closes #1452, thanks @sergio-sisternes-epam)
- `apm compile --target claude` omits the "Project Standards" section from `CLAUDE.md` when instructions are already deployed to `.claude/rules/` by `apm install`, avoiding duplicate content in Claude Code's context window. `CLAUDE.md` is still generated for constitution and dependency imports. (closes #1138, #1146)
- `apm compile --watch` now live-reloads `apm.yml` edits instead of caching the initial snapshot, and warns when combined with `--clean` so a watch session does not silently wipe state on every change. (#1403)
- Windows: `_local_path_from_source` handles `file://` URI shapes (drive-letter, UNC) so local marketplaces on Windows resolve correctly, and preserves POSIX separators on plain POSIX-shaped inputs (`Path().expanduser()` was rewriting `/home/user` to `\home\user`); local-path pass-through is now cross-platform. (#1484)
- `apm install` rewrites skill-shipped MCP `command` paths for the Claude target so portable installs resolve relative to `.claude/` instead of the source skill's checkout. (#1465)
- Unstuck 3 flaky integration tests that were intermittently blocking the merge queue. (#1477)

## [0.14.2] - 2026-05-22

### Added

- **Experimental:** `copilot-app` target scopes workflows to a real project row via loopback WS IPC (App running) or direct SQLite (App closed); `--global` workflow installs emit a one-time CWD-pivot warning. (#1431)
- `apm pack --marketplace=FORMATS` filters which marketplace formats build in a single run; accepts comma-separated names and `all`/`none` sentinels. (#1324)
- `apm pack --marketplace-path FORMAT=PATH` overrides the output path for a specific marketplace format at invocation time. (#1324)
- `apm pack --json` emits a stable JSON contract to stdout (`{ok, dry_run, warnings, errors, marketplace: {outputs: [...]}}`); all logs move to stderr so downstream tooling can `jq` the output. (#1324)
- `marketplace.outputs` in `apm.yml` accepts a map form keyed by format name (`outputs: {claude: {}, codex: {path: ...}}`), replacing the deprecated list form; the list form still parses with a one-cycle deprecation warning. `apm marketplace init` now scaffolds the explicit map form. (#1324)

### Changed

- PR-time CI shards unit tests, moves the 80% coverage gate off the critical path, and parallelises the PyInstaller build for faster contributor signal. (#1437)
- Unit coverage raised to 88% (gate 80%); integration coverage raised to 71% (gate 55%). (#1402)
- Replace logic-replay tests for #763 with real-flow coverage to catch real regressions. (#1340)

### Performance

- Cold `apm install` for subdirectory git deps is ~30x-75x faster on large monorepos via partial bare clones plus sparse-cone materialization; transparent fallback when servers reject `--filter=blob:none`. (#1436, closes #1433)

### Fixed

- Windows: GitCache sparse-cone consumer clone no longer fails with `Filename too long`; `git clone`/`checkout` pass `-c core.longpaths=true` on Windows so the nested `checkouts_v1/<shard>/<sha>/<variant>.incomplete.<pid>.<ns>/` layout stays within the Win32 path limit. (#1454)
- Windows: `copilot-app` WS handshake no longer rejects `~/.copilot/run/ws.token` because Windows synthesises group/other bits from the read-only flag; the POSIX-only mode check now short-circuits to accept on Windows. (#1454)
- `apm install` honours per-dependency `registry:` URLs in MCP deps instead of warning and ignoring them. (#1443, closes #1393)
- VS Code adapter handles MCP v0.1 `runtimeArguments` with `variables`, unblocking Docker MCP servers that need workspace mounts. (#1444, closes #1391)
- `apm install --skill <name>` persists the skill filter to `apm.yml` so subsequent runs honour the selection. (#1442, closes #1395)
- `apm audit` no longer false-flags non-skill primitives as orphaned drift on `auto_create=False` targets. (#1441, closes #1411)
- Copilot harness auto-detection recognises `.github/instructions/`, `agents/`, `prompts/`, and `hooks/` as valid markers. (#1440, closes #1435)
- `apm install` (project-scope) keeps hook `command` paths repo-relative so committed configs stay portable across clones and CI. (#1396, closes #1394)
- PyInstaller binary restores `optimize=2`, shrinking the Windows Defender ML false-positive surface. (#1450)
- Comma-separated `applyTo` globs emit proper YAML lists in Claude, Cursor, and Windsurf instruction outputs (brace-aware split). (#1387, #1449, closes #1366)
- `GitCache` no longer crashes on Windows `file://` URLs where `urllib.parse.port` raises `ValueError`. (#1446)
- `apm compile` discovers local-bundle instructions inside `apm_modules/` again. (#1388)
- `apm install --target copilot-app` warns instead of failing when the App schema is newer than expected. (#1434)
- Plugin packaging no longer destroys pre-positioned `.apm/` content during artifact mapping. (#1416)
- `apm update` against private ADO deps no longer fails on Windows with a misleading "az not logged in" -- `az.cmd` is now resolved via `shutil.which`; ADO preflight preserves `GIT_*` env so GCM can fall back. (#1432, closes #1430)
- Windows unit-test job is green again (Codex CLI path quoting + ADO subprocess env). (#1427)
- Root `.apm` hooks no longer duplicate after directory renames or worktrees; hook source-ids derive from `apm.yml` `name` and self-heal stale entries. (#1392, closes #1329)
- Codex and Gemini stdio MCP configs expand `${env:VAR}` placeholders in self-defined env vars. (#1277, closes #1266)
- Codex CLI now supports Streamable HTTP MCP servers, matching the other harnesses. (#1262, closes #1260)
- `apm install` preflight uses ssh (not https) on ssh-based dependency URLs. (#1303, closes #1293)

## [0.14.1] - 2026-05-20

### Added

- **Experimental:** `copilot-app` target deploys prompts (with optional workflow frontmatter) directly into the GitHub Copilot desktop App's `~/.copilot/data.db` workflows table; shape-dispatched `.prompt.md` (workflow keys `interval` / `schedule_hour` / `schedule_day` route to the App, plain prompts route to slash-command targets) so each prompt lands on exactly one surface. Gated behind `apm experimental enable copilot-app`; works in project (`apm install --target copilot-app`) and user (`--global`) scope; workflows install disabled, user opts in from the App. See [Copilot App integration](https://microsoft.github.io/apm/integrations/copilot-app/). (#1405)

### Fixed

- `apm install` honors the SSH user portion of dependency URLs (`ssh://user@host/...` and scp shorthand `user@host:org/repo`) instead of hardcoding `git@`; unblocks EMU accounts and other non-`git` SSH identities. (#1385, closes #1383)
- Windows installer and `apm self-update` detect Windows Defender / antivirus blocks (HRESULT `0x800700E1`, PUA messages) and surface three actionable recovery options (`Add-MpPreference -ExclusionPath`, `pip --user`, false-positive submission URL) instead of falling through to a generic "failed to run" and a pip fallback that itself dies on unsupported Python. (#1408)
- Windows installer and `apm self-update` survive AppLocker / App Control for Business (WDAC) policies by staging the release into the allow-listed per-user install root before running the `apm.exe --version` smoke test, and emit AppLocker-specific guidance on `0x80070005` Access Denied instead of silently falling back to pip. (#1390, closes #1389)
- `apm uninstall <local-path>` on Windows no longer rejects absolute paths (`C:\...\my-pkg`) as "Invalid package format" and silently leaves deployed copilot-app workflow DB rows behind. Local paths are now detected on every platform, so install/uninstall round-trips cleanly. (#1413)

### Changed

- `apm compile --watch` picks up mid-session edits to `apm.yml`'s `target:` / `targets:` on the next file event instead of caching the resolved target until the watcher is restarted; previously the value resolved at startup was reused on every recompile. Follow-up to #1349. (#1403)
- `apm compile --watch --clean` prints an explicit `[!]` warning that `--clean` is ignored in watch mode and continues; previously the flag was silently dropped. Run `apm compile --clean` separately between watch sessions to remove orphaned outputs. (#1403)

## [0.14.0] - 2026-05-18

### Breaking

- **MCP Registry v0.1**: self-hosted registries must serve `/v0.1/` paths (legacy `/v0/`-only registries return 404). `api.mcp.github.com` and `registry.modelcontextprotocol.io` are unaffected. Python API: `get_server_info(uuid)` -> `get_server(server_name, version)`; old name remains one minor as `DeprecationWarning`. Thanks @fassmus for the report. (#1337, closes #1210)

### Added

- `apm plugin init` is the canonical noun-verb command for scaffolding a plugin project; composes with `apm marketplace init` so single-plugin, aggregator, and hybrid repo shapes emerge from verb composition (no `--shape` flag). New docs: [Repo shapes](https://microsoft.github.io/apm/producer/repo-shapes/), [Releasing from any CI](https://microsoft.github.io/apm/producer/releasing-from-any-ci/), [Versioning strategies](https://microsoft.github.io/apm/producer/versioning-strategies/). (#1370)
- `apm init` prints a "Next steps" panel surfacing both `apm plugin init` and `apm marketplace init` alongside consumer commands, teaching the noun-verb taxonomy at zero migration cost. (#1370)
- `apm pack --check-versions` validates per-package versions against `marketplace.versioning.strategy` (`lockstep` / `tag_pattern` / `per_package`); exits `3` on misalignment. (#1365)
- `apm pack --check-clean` regenerates each `marketplace.json` into a temp dir and diffs against the committed copy; exits `4` on drift. Never writes to the working tree. (#1365)
- `apm.yml` schema: new optional `marketplace.versioning: { strategy: ... }` block (default `lockstep`); existing manifests unaffected. (#1365)
- `apm marketplace doctor` adds `format coverage` and `version alignment` rows surfacing missing supported outputs and per-package drift. (#1362, #1365)
- `apm pack --marketplace=FORMATS` filters which marketplace formats build in a run; accepts comma-separated names and sentinels `all`/`none`. (#1317)
- `apm pack --marketplace-path FORMAT=PATH` overrides the output path for a specific format at invocation time. (#1317)
- `apm pack --json` emits a stable JSON contract on stdout; all logs move to stderr so tooling can `jq` the output safely. (#1317)
- `marketplace.outputs` in `apm.yml` accepts map form `outputs: {claude: {}, codex: {path: ...}}` (the deprecated list form still parses with warning). (#1317)
- `apm uninstall` accepts marketplace notation (`my-plugin@official`), symmetric with `apm install`; offline-first via lockfile with registry fallback guarded against canonical injection. (#1325)
- `apm update` adds `--target/-t` to scope refresh to a single agent. (#1358)
- `install.ps1` accepts air-gapped / GHES env vars (`VERSION`, `GITHUB_URL`, `APM_REPO`, `APM_INSTALL_DIR`, `APM_SKIP_CHECKSUM`) for Windows pinned installs, matching `install.sh`. (#1246)
- Prefer APM-managed runtime binaries over system PATH; warn at setup time on Codex >= v0.116 + GitHub Models incompatibility instead of failing silently later. (#1356)

### Changed

- **`apm update` is roughly two orders of magnitude faster on multi-dep manifests**: a four-tier resolver (in-memory cache -> GitHub commits API -> bare `rev-parse` -> legacy clone) collapses redundant `git clone --depth=1` calls. Same wiring benefits install, outdated, and publish. (#1376, closes #1369)
- `shared/apm.md` gh-aw workflow no longer fails with `no apm bundles found` for form-2 (single-app) and form-3 (`apps[]`) consumers: GitHub Actions was silently stripping the private-key PEM from the `apm-prep` job's matrix output as a secret leak. (#1373)
- `apm pack` appends a vendor-neutral catalog after per-output success listing each marketplace artifact and a single docs anchor; never names a vendor CLI surface. (#1362)
- `apm marketplace init` scaffolds map-form `outputs: {claude: {}}` with a single-line `# codex: {}` commented toggle; flip one line to enable Codex output. (#1362)
- `apm install` from `*.ghe.com` (GHE Cloud) marketplaces routes auth at the registered enterprise host instead of silently defaulting to `github.com` and 401'ing; the resulting `apm.yml` records the correct enterprise `git:` URL. (#1292)
- `apm install` from `*.ghe.com` marketplaces surfaces a warning-level hint when a bare-`repo` cross-repo plugin source fails validation, naming the enterprise host and the host-qualified `repo` value to set. (#1319)
- `extends: org` correctly layers `dependencies.require` / `dependencies.deny` and `unmanaged_files` when the child omits the block entirely (`None` = transparent, `[]` = explicit override). (#1290, #1248)
- `--marketplace-output PATH` is hidden from `--help` and emits a stderr deprecation warning; auto-translates to `--marketplace-path claude=PATH`. (#1317)
- `apm view --help` and the `view` row in `apm --help` render in release binaries again; PyInstaller `optimize=2` was stripping `__doc__` from every Click command. (#1307)

### Deprecated

- `apm init --plugin` and `apm init --marketplace` are deprecated in favor of `apm plugin init` and `apm marketplace init`; legacy flags still work and print a one-line stderr warning, **removal in v0.16**. (#1370)

### Fixed

- `apm compile --watch` honors `targets: [...]` on every recompile instead of silently regenerating all-target outputs. (#1349, closes #1345)
- `apm install` no longer permanently blocks installs gated by `required-packages-deployed` after a degraded `deployed_files=[]` lockfile: non-skill integrators silently adopt byte-identical pre-existing files. (#1313)
- `apm audit` drift check returns skip-with-info (`passed=True`) when the install cache is cold, instead of failing the audit; CI pipelines that have not yet run `apm install` are not incorrectly red-marked. (#1289)
- `apm pack` no longer prints a misleading "No plugin.json found" warning for marketplace-publishing projects. (#1362)
- `apm marketplace init` scaffolds the snake_case `tag_pattern: "{name}-v{version}"` example instead of the schema-invalid camelCase `tagPattern`. (#1362)
- `apm install` respects the `targets:` whitelist for MCP servers exactly like skills, dropping non-listed runtimes even when their on-disk signal exists; greenfield projects (no `targets:`, no `--target`, no signals) now error consistently between `apm install` and MCP-only invocations. (#1336, closes #1335)
- Gemini CLI: `apm install -g --mcp NAME` writes to `~/.gemini/settings.json` (user scope); project-scope writes land at `<project_root>/.gemini/settings.json` instead of `cwd`. (#1306, closes #1299)
- Claude target: hook commands with relative paths now resolve to absolute paths in `settings.json`. (#1354, closes #1310)
- Claude target: hook ownership metadata is stored in a `.claude/apm-hooks.json` sidecar so APM can track owned hooks without violating the Claude `additionalProperties` schema. (#1359, closes #1279)
- Claude target: `apm install` preserves self-defined stdio MCP `env` values from `apm.yml` and stringifies non-string values like `PORT: 3000` for MCP compatibility. (#1224, closes #1222)
- Direct GitHub API and ADO/GHES `git ls-remote` calls now respect `PROXY_REGISTRY_ONLY` mode; all four validation paths skip outbound network probes. (#1357, closes #615)
- Target propagation no longer drops mid-pipeline at the intermediate `CompilationConfig` stage. (#1355, closes #765)
- `apm pack` and `apm install` now warn when `apm.yml` is missing but APM artifacts exist on disk. (#1255, closes #1056)
- `apm install` accepts Bitbucket Data Center / Server personal-repo URLs containing `~` (e.g. `https://example.com/scm/~user/repo.git`); the path-component whitelist on non-ADO hosts now includes `~` (RFC 3986 unreserved). Sourcehut `~user` paths are incidentally unblocked too. (#1377, closes #1375)

## [0.13.0] - 2026-05-11

### Breaking Changes

- The CLI self-updater moved from `apm update` to `apm self-update`. Inside an `apm.yml` project the bare `apm update` verb now refreshes project dependencies (matching `npm update`, `uv lock --upgrade`, `cargo update`); outside a project it forwards to `apm self-update` with a deprecation banner for one release. CI scripts that called `apm update` to refresh the binary should migrate now: `sed -i 's/apm update/apm self-update/g' your_scripts`. (#1244)

### Added

- `apm update` refreshes project dependencies the way npm/pip/cargo users expect: resolves `apm.yml` against latest refs, shows an added/updated/removed/unchanged plan, and prompts before mutating anything (`--yes` skips, `--dry-run` previews). (#1244)
- `apm self-update` updates the APM CLI binary itself (or shows distributor guidance when self-update is disabled at build time); `--check` only checks for a newer version. (#1244)
- `apm install --frozen` performs a CI-safe, read-only install that fails fast (exit 1) when `apm.lock.yaml` is missing or out of sync with `apm.yml`; mutually exclusive with `--update`. (#1244)
- Zero-config private-package auth on github.com, `*.ghe.com`, and GHES when the `gh` CLI is logged in: APM uses `gh auth token --hostname <host>` before falling back to `git credential fill`. (#630)
- GitLab marketplace and install support: `gitlab.com` and self-managed instances (via `GITLAB_HOST` / `APM_GITLAB_HOSTS`) use GitLab REST v4 for `marketplace.json` and raw file reads; nested group paths are disambiguated via object-form `git:` + `path:`. (#1149)
- **Windows installer parity:** CI and GHES/air-gapped runners can **pin `VERSION`**, set **`GITHUB_URL`** (https only) and **`APM_REPO`**, and use **`APM_INSTALL_DIR`** like `install.sh`. Pinned installs verify the release **`.sha256`** unless **`APM_SKIP_CHECKSUM=1`** or **`-SkipChecksum`** (emergency only); `GITHUB_URL` drives GitHub.com (`api.github.com`) vs GHES (`{host}/api/v3`) for latest discovery. (#668)
- Virtual subdirectory and raw-file packages now resolve from self-hosted Git services (Gitea, Gogs) via raw URL with API v1/v3 fallback. (#587)
- `git: parent` lets packages in a git monorepo reference sibling paths via `{ git: parent, path: ... }` without repeating the full `git:` URL; the lockfile stores expanded host, repo, and resolved ref like every other virtual git dependency. (#1149)
- `shared/apm.md` gh-aw workflow exposes a `target:` import input (default `all`) so consumer workflows can ship slim, single-harness bundles. (#1184)

### Changed

- `apm marketplace browse/search/add/update` route through the registry proxy when `PROXY_REGISTRY_URL` is set; `PROXY_REGISTRY_ONLY=1` blocks direct GitHub and GitLab host fallbacks; a plaintext-bearer warning fires on `http://` proxies unless `PROXY_REGISTRY_ALLOW_HTTP=1` is set. (#1149)
- `apm install --force` help text now states the flag does NOT refresh refs; the no-op nudge now reads "Lockfile already satisfied -- run `apm update` to resolve latest refs"; a one-time CI banner fires when `apm update` runs under `CI`/`GITHUB_ACTIONS` so pipelines see the dependency-refresh shift. (#1244)
- Tier-2 smoke job runs `tests/integration/test_core_smoke.py` against the built binary, exercising `init` / `install` / `compile` / `audit` / `policy status` to fail fast on the README promises; replaces `test_runtime_smoke.py`. (#1251)
- Integration tests now use marker-driven discovery: `requires_*` markers replace 21 `pytestmark = pytest.mark.skipif(...)` chains, `scripts/test-integration.sh` is a thin orchestrator, and new test files dropped into `tests/integration/` are picked up automatically. (#1167, #1247, #1249)
- Integration test apm-binary resolution prefers the local build (`./dist/apm-<os>-<arch>/apm`) over a system-wide `apm` on `PATH`, so contributors validating the binary under test are not silently shadowed by a global install. (#1167)
- Integration Tests merge-queue job is now sharded 4-way via pytest-split with `-n 2` xdist per shard, cutting wall-clock from ~30 min to ~6 min while keeping HOME-mutating tests race-safe. (#1263)

### Fixed

- `apm install` no longer silently overwrites pre-existing governance files; `check_collision()` now treats `managed_files=None` (first install, no lockfile) as an empty set so hand-rolled files in `.github/instructions/` are detected and protected. (#1256)
- Policy inheritance now fails closed: child policies that omit `unmanaged_files` inherit the parent's action instead of silently defaulting to `ignore`. (#1253)
- MCP server token injection now requires both an allowlisted server name and a verified HTTPS GitHub hostname, preventing PAT exfiltration via poisoned registry entries. (#1239)
- `apm install --target cursor` now emits Cursor-native MCP schema (`type: stdio` / `type: http`) instead of Copilot-only fields that Cursor silently discards; `.cursor/mcp.json` is gitignored to prevent accidental token commits. (#1240)
- `apm install` accepts full ADO HTTPS URLs with sub-path virtual packages (e.g. `https://dev.azure.com/org/proj/_git/repo/sub/path`) instead of rejecting them. (#1254)
- `apm install` no longer crashes with exit 128 when a mono-repo package depends on a sibling pinned to a non-HEAD commit; multiple SHA-pinned refs to the same repository now share a single cached clone. (#1258, #1259)
- `apm install --update` falls back from a stale `ADO_APM_PAT` to an `az login` AAD bearer in the preflight auth probe, matching the behavior of `apm install`; per-host stale-PAT warnings are lock-guarded so parallel installs emit one warning instead of one-per-thread. (#1212, #1214)
- `apm install` rejects unsupported flat-format `dependencies` (e.g. `dependencies: [owner/repo]`) with a clear error and structured-format hint instead of silently ignoring them. (#1189)
- `apm install` accepts the YAML list form under `target:` (e.g. `target: [copilot, claude]`); previously crashed with a garbled `Unknown target` error. (#1197)
- `apm install --target all` no longer enumerates the experimental `copilot-cowork` target, which was crashing project-scope installs and making `gh aw` workflows that pin `target: all` unusable. (#1191)
- `apm install --global <abs-local-path>` no longer rejects absolute local paths at user scope (regression from #1149); restores the post-#937 contract that only relative local paths are ambiguous at user scope. (#1247)
- `apm install` against a `--global` scope now writes MCP entries to the scope-resolved lockfile path instead of the project lockfile. (#1236)
- `apm install <git+https://...>` direct refs now apply the same manifest-driven credential validation as `apm.yml`-declared dependencies, closing a silent auth-fallback gap. (#1242)
- Dependency references no longer treat `:443`/`:80`/`:22` as distinct from default-scheme ports, so two manifest entries that differ only by an explicit default port resolve to the same package. (#1237)
- The experimental `cowork` config key is hidden from `apm config` output, and the verbose target-resolution log no longer leaks internal cowork plumbing. (#1241)
- Multi-account Git Credential Manager users: APM selects the right GitHub account automatically per repository (no account-picker prompt) when `credential.useHttpPath = true` is set. (#1226)
- `apm install` target-agnostic local bundles: `apm pack` no longer hardcodes `pack.target` into bundles; `apm install <bundle>` resolves the consumer target from project context and wires `.mcp.json` servers per target. (#1207, #1217)
- `Unknown target` error suggestions no longer advertise the `agent-skills` meta-target (which `apm targets` intentionally omits); the canonical set still accepts `agent-skills` via `--target` and `apm.yml`. (#1208, #1215)
- `apm outdated --help` now renders the missing one-line description. (#1216)
- `shared/apm.md` no longer wraps the `target` input in a `|| 'all'` fallback that broke gh-aw's expression-substitution regex and silently dropped consumer-supplied `target:` values. (#1185, #1186)
- GitLab monorepo marketplaces: `apm install plugin@marketplace` resolves plugins whose sources live in a subdirectory of the marketplace repository on GitLab-class hosts. (#1149)
- Protocol-fallback port warnings are deduplicated across parallel download workers via a threading lock, so each (host, repo, port) triple warns exactly once. (#1238)
- `apm marketplace add` accepts GitLab-class hosts; unsupported generic hosts now show separate recovery hints for GHES (`GITHUB_HOST`) and self-managed GitLab instead of only `GITHUB_HOST`. (#1149)
- Realigned the integration suite with current product contracts: copilot-target detection requires `.github/copilot-instructions.md` (post-#1154), `apm marketplace build` is removed in favor of `apm pack`, ADO virtual collections use the SUBDIRECTORY layout (post-#1094), and the `repo:` apm.yml key is replaced by `git:`. (#1257, #1261, #1264)
- `triage-panel` scheduled sweep now paginates oldest-first via GitHub MCP `search_issues` with `-label:status/triaged sort:created-asc`, so daily runs drain the untriaged backlog instead of processing one issue per cron tick. (#1193, #1194)
- **Policy `unmanaged_files`:** Child policies that omit the block (or use an empty mapping) under `extends:` inherit the parent org settings transparently, fixing silent downgrades such as losing `action: deny`; values that are not YAML mappings (lists or scalars) are rejected with a recovery hint. (#1198, #1248)

## [0.12.4] - 2026-05-07

### Fixed

- `apm install` now removes deployed files when a package is removed from `apm.yml`. Three sequential early-returns previously short-circuited the cleanup phase when the manifest was emptied; the orphan-cleanup logic itself was correct. (#1173)
- `apm audit --ci` no longer reports false drift on self-package primitives that link to repo-root files (`[..](../../FILE.md)`). The replay's in-package asset rewriter now re-anchors `target_location` onto `package_root` when the candidate sits outside the scratch tree, mirroring real-install output. (#1182)

## [0.12.3] - 2026-05-06

### Security

- `apm install --target copilot` no longer bakes secret values into `~/.copilot/mcp-config.json`: env-var placeholders (`${env:VAR}`, `${VAR}`, legacy `<VAR>`) are translated to Copilot's native `${VAR}` runtime form so secrets never touch disk. Rotate any previously-baked secrets and re-run install. (#1169, closes #1152)

### Changed

- Explicit, auditable target resolution: `apm install` / `apm compile` follow a strict `--target` > `apm.yml target:` > auto-detect chain, print a `[i] Targets: ... (source: ...)` provenance line, and exit 2 on empty repos instead of silently defaulting to `copilot`. Adds `apm targets` discovery command and `apm compile --all` (deprecates `--target all`). (#1165, closes #1154, #1122, #1130, #518, #888, #891, #650, #1056)
- `apm init` opens an interactive numbered-toggle target checklist pre-seeded from filesystem signals so users land in Tier 2 (`apm.yml target:`) by default; adds `--target` for scripted use. (#1165)
- `apm install` honours `policy.fetch_failure_default: block` for `no_git_remote` / `absent` / `empty`, matching the audit behaviour. (#1164)

### Fixed

- `apm audit --ci` no longer silently passes when no org policy is resolved: auto-discovery warns on stderr and honours `policy.fetch_failure_default: block` to fail closed (exit 1); JSON/SARIF on stdout stays clean. (#1164, closes #1159)
- SCP-shorthand SSH URLs from non-`git` users -- `<user>@github.com:owner/repo` (EMU) and `<user>@ssh.dev.azure.com:v3/<org>/<project>/<repo>` (ADO) -- now parse correctly in dependency parsing and policy auto-discovery. (#1164)
- `apm install` against a branch ref re-downloads when upstream advances past the lockfile-recorded SHA, and self-heals lockfiles produced by APM <= 0.12.2 on next install. (#1158)
- In-package relative markdown links are rewritten to their `apm_modules/` location at install time so sibling references survive the `.agents/.github` deploy split. (#1160, closes #1147)
- `.apm-pin` cache marker no longer leaks into skill deploy targets on subsequent installs. (#1153)

## [0.12.2] - 2026-05-05

### Added

- **`apm audit` now catches forgotten installs and hand-edits by default.** No more shipping stale `.github/instructions/` because someone forgot to re-run `apm install`, no more silent hand-edits to regenerated content. Opt out with `--no-drift`. See the [Drift Detection guide](https://danielmeppiel.github.io/awd-cli/guides/drift-detection/). (#1137, closes #1071, supersedes scope of #898)

### Fixed

- **Parallel subdir install race.** `apm install` no longer intermittently fails with `RuntimeError: Subdirectory '<path>' not found in repository` when multiple dependencies (including ADO sub-path deps) resolve to different subdirectories of the same `repo@ref`. The shared clone cache now stores subdir-agnostic bare clones and each consumer materializes its own working tree (mirrors the WS3 `GitCache` pattern). (#1135, fixes #1126, fixes #1140)
- **Re-installing the same package no longer rmtree's it.** `compute_package_hash` now excludes the `.apm-pin` cache marker (introduced by #1137) so the supply-chain content-hash check sees a stable hash across installs instead of falsely tripping and deleting `apm_modules/<owner>/<pkg>/`. (#1142, regression from #1137)

### Changed

- **Docs: `first-package` guide accuracy.** Clarifies how `includes: auto` actually behaves and corrects the skill deployment paths so a newcomer's first package matches what `apm install` writes to disk. (#1129)
- **Docs: APM's role for skills, plugins-as-packages, ADO sub-paths.** Six user-surfaced doc gaps closed in one pass -- unifies the "plugin == package" framing, adds ADO sub-path install examples, and states the current stability posture so users stop guessing from release notes. (#1127)

## [0.12.1] - 2026-05-03

### Added

- **Cursor slash command support (Cursor 1.6+).** `apm install` deploys package `.prompt.md` files to `.cursor/commands/*.md` when a `.cursor/` directory is present, completing cross-tool slash-command parity with Claude Code, OpenCode, and Gemini CLI. (#1046)
- **`apm install` performance + UX overhaul:** WS3 persistent two-tier cache for git + HTTP (with rev-parse HEAD verification on cache hits), parallel level-batched BFS dependency resolution, parallel MCP registry batch lookups with ETag-revalidated HTTP cache, in-install repo clone dedup for subdirectory deps, reflink-aware file copies with write-dedup for cache checkouts, live progress UI for parallel resolution and download, per-phase timing in `--verbose`, elapsed time on every exit path, and ASCII-only progress bar. (#1116)

### Fixed

- **gh-aw lock workflows: bump `microsoft/apm-action` v1.5.0 -> v1.6.0** so packed bundles use the legacy `--format apm` layout that `apm unpack` accepts after the v0.12.0 default flip to plugin format. (#1121)
- **Windows CI: explicit UTF-8 encoding** in the `_make_package` test helper to unblock Windows runners. (#1124)

## [0.12.0] - 2026-05-03

### Added

- **`--target agent-skills` deploys skills to `.agents/skills/` (cross-client shared directory).** The new target writes `SKILL.md` files to the [agentskills.io](https://agentskills.io) standard location without tying them to a single client. Excluded from `--target all` (explicit opt-in only); combine with `--target all,agent-skills` for both. Deduplicates with Codex when both targets resolve to the same path. User-scope (`-g`) deploys to `~/.agents/skills/`. (closes #737)
- **Claude Code as MCP install target.** `apm install --runtime claude` (and auto-detection when `.claude/` exists or the `claude` binary is on PATH) writes MCP server entries to project-scope `.mcp.json` and user-scope `~/.claude.json` per the [Claude Code MCP schema](https://code.claude.com/docs/en/mcp). Project writes are opt-in (gated on the presence of `.claude/`, mirroring Cursor/OpenCode) and user writes are atomic with `0o600` permissions on first creation so concurrent Claude Code sessions cannot truncate the shared config or leak embedded OAuth state. Stdio entries are emitted with explicit `type: "stdio"` so `claude mcp list` renders them identically to entries installed via `claude mcp add --transport stdio`. Schema fidelity is regression-locked by golden fixtures captured live from the upstream `claude` CLI (see `tests/integration/test_claude_mcp_schema_fidelity.py`). Stale-cleanup defaults conservatively when the install scope is unspecified -- only the project file is touched without an explicit `-g/--global` opt-in. Note: Claude Code's third LOCAL scope (per-project private under `~/.claude.json -> projects.<path>.mcpServers`) is intentionally not implemented, since APM packages target reproducible team installs (PROJECT/USER), not per-user-per-project private state. (#1104, closes #643) -- by @dmartinol
- **`apm install <local-bundle-path>` deploys a previously packed bundle (directory or `.tar.gz`) air-gapped, with sha256 integrity verification against the bundle's embedded `apm.lock.yaml`.** Honours `--target`, `--global`, `--force`, `--dry-run`, plus a new `--as ALIAS` log/display label flag. Local installs record written paths in the project lockfile (`local_deployed_files` / `local_deployed_file_hashes`) and never mutate `apm.yml`. `apm unpack` is now `[Deprecated]` and points at this entrypoint. (#1098)
- **`apm pack` now embeds `apm.lock.yaml` inside the bundle** with two new `pack:` fields (`target`, `bundle_files`: a `path -> sha256` manifest) so installs can verify integrity and detect target mismatches before writing any file. (#1098)
- **`apm compile -t copilot` now emits `.github/copilot-instructions.md` with zero user configuration** -- APM's first Copilot-native compile target. Global instructions in `.apm/instructions/` are assembled into the file VS Code and GitHub Copilot read automatically; switching targets cleans it up. APM dogfoods this target. (#1048)
- **`apm marketplace add` accepts full HTTPS URLs and nested HOST/group/sub/.../REPO shorthands.** You can now paste a repository URL straight from the browser (e.g., `apm marketplace add https://github.com/acme/plugin-marketplace`) and register marketplaces hosted under nested sub-paths on GitHub Enterprise (`ghes.corp.example.com/org/team/repo`). Path-traversal sequences in the parsed segments are rejected via `validate_path_segments`. Non-GitHub hosts (GitLab, Bitbucket, etc.) are explicitly rejected at registration time with an actionable error -- this avoids forwarding GitHub credentials to unintended hosts and the silent fetch-time 404 that previously resulted; native non-GitHub support is tracked separately. (#1034, closes #1027)
- Regression tests for `apm compile` placement of narrow `applyTo` patterns: instructions whose matches all live deep inside one subtree are now pinned to the deepest covering directory instead of being hoisted to the project root, across both selective and single-point placement strategies. Also covers the file-walk cache that skips repeated filesystem scans for the same glob. (#871)
- **`apm marketplace audit <name>`** -- detect supply-chain risk in a marketplace with one command. It fetches each plugin's own `apm.yml` at its pinned ref and warns when transitive `dependencies.apm` entries bypass marketplace pinning. Default run is informational and exits 0; `--strict` exits non-zero on bypass warnings or unverifiable plugins, for use in CI. Complements the existing `apm marketplace doctor` environment diagnostics. (#881)
- **`apm pack` marketplace builder hardening.** Local source paths are now emitted relative to `metadata.pluginRoot` (fixes double-prefix bug). New pass-through fields: `author`, `license`, `repository`, `keywords` (alias for `tags`). Curator-wins override semantics for `description`/`version` on remote entries. Security guards reject path traversal and absolute paths post-subtraction. (#1061)
- **Plugin manifest schema-conformance tests.** `tests/unit/test_plugin_exporter_schema.py` validates every shape of `plugin.json` produced by `apm pack` (synthesized, authored, and authored-with-stale-keys) against the vendored official schema. Companion marketplace conformance lives in `tests/unit/marketplace/test_schema_conformance.py`. (#1061)
- **APM now compiles and integrates to Windsurf/Cascade.** New first-class `--target windsurf` support: instructions deploy as `.windsurf/rules/` with trigger frontmatter, agents deploy as `.windsurf/skills/<name>/SKILL.md`, commands as `.windsurf/workflows/`, hooks merge into `.windsurf/hooks.json`, and MCP servers configure via `~/.codeium/windsurf/mcp_config.json`. Auto-detection, user-scope deployment, and `apm pack` all support the new target. (#1066)
- Slash commands installed from APM packages now surface argument hints in Claude Code -- `apm install` automatically maps prompt `input:` to Claude's `arguments:` front-matter, rewrites `${input:name}` references to `$name`, and auto-generates `argument-hint`. Argument names are validated against an allowlist to prevent YAML injection from third-party packages, and the mapping is reported at install time. (#1039)

### Changed

- **Skills for Copilot, Cursor, OpenCode, and Codex now deploy to `.agents/skills/` by default (skill routing convergence).** The four clients whose documentation lists `.agents/` as a skill discovery path now share a single deployment directory, eliminating up to 4 redundant copies per skill when `--target all` is used. Claude retains its native per-client routing (`.claude/skills/`); Gemini joins the convergence in the entry below. Pass `--legacy-skill-paths` (or set `APM_LEGACY_SKILL_PATHS=1`) to restore the previous per-client layout. (closes #1103)
- **Gemini CLI joins the `.agents/skills/` convergence by default.** Gemini CLI docs list `.agents/skills/` as the preferred alias over `.gemini/skills/`, so Gemini now shares the converged deployment directory with Copilot, Cursor, OpenCode, and Codex (5 clients total). Claude remains on its native `.claude/skills/` routing. (#737)
- **Auto-migration**: `apm install` now migrates legacy per-client skill deployments (`.github/skills/`, `.cursor/skills/`, `.opencode/skills/`, `.gemini/skills/`) recorded in `apm.lock.yaml` to `.agents/skills/`. Foreign / hand-authored skills outside the lockfile are left untouched. Use `--legacy-skill-paths` to opt out. (#737)
- **Renamed `NOTICE.md` -> `NOTICE`** to follow the Apache / CNCF convention used by upstream third-party-attribution files (e.g. `kubernetes-sigs/kro`, `kubernetes-sigs/headlamp`). The generator (`scripts/generate-notice.py`), `make notice` target, and `NOTICE Drift Check` workflow now operate on the extension-less path. (#1073)
- **NOTICE: added "Submitted on behalf of a third-party" section** crediting five contributors whose pull requests landed before the `microsoft-github-policy-service` CLA bot recorded a signature on file -- in keeping with the section-7 wording adopted by CNCF NOTICE files. Driven by a new `_third_party_submissions` block in `scripts/notice-metadata.yaml`. (#1073)
- **BREAKING: `apm pack` now produces a Claude Code plugin directory by default — zero extra flags, schema-validated `plugin.json`, convention dirs auto-discovered.** The legacy APM bundle layout is preserved under `--format apm`. Migration: CI workflows and scripts that consume the legacy bundle must add `--format apm` (the [`microsoft/apm-action`](https://github.com/microsoft/apm-action) wrapper has been updated accordingly). (#1061)
- **Plugin manifest schema conformance.** The synthesized/written `plugin.json` no longer emits `agents`/`skills`/`commands`/`instructions` keys pointing at the convention directories — these are auto-discovered by Claude Code, and per the [official schema](https://json.schemastore.org/claude-code-plugin.json) those array entries must be `./*.md` paths to *additional* files. The convention dirs themselves are still copied to disk. When stripping such keys from an authored `plugin.json`, `apm pack` now emits a warning so authors can clean up their source. (#1061)

### Deprecated

- **`--target agents` is deprecated** -- it maps to `copilot` (`.github/`), not `.agents/`. Use `--target copilot` or `--target agent-skills`. Removal in v1.0. (closes #627, #640, #664)

### Removed

- **BREAKING: dropped support for `.collection.yml` / `.collection.yaml` virtual packages.** Dependencies whose paths end in `.collection.yml` or `.collection.yaml` now raise a `ValueError` at parse time with a migration message. Convert any such entry to a regular `apm.yml` with a `dependencies:` section under the same subdirectory, then reference the directory itself as a subdirectory virtual package (no extension). The internal `VirtualPackageType.COLLECTION` enum value, the `download_collection_package` codepath, the `is_virtual_collection()` reference helper, and the `META_PACKAGE` package-type label have been removed -- none were persisted to the lockfile, so existing locks are unaffected. (#1097, closes #1094) Thanks @edenfunf for the original PR.

### Fixed

- **`apm install` now anchors transitive `local_path` deps on the declaring package's directory (npm/pip/cargo parity).** Sibling/monorepo layouts (e.g. `../base` declared inside `packages/specialized/apm.yml`) now resolve relative to the declaring package, not the consumer's project root. **Security tightening:** remote-cloned packages can no longer declare `local_path` deps -- both relative and absolute paths are rejected at `ERROR` severity at resolve time. (#1111, closes #857) Thanks @JahanzaibTayyab.
- `apm compile` no longer silently drops instructions without an `applyTo` pattern from generated `AGENTS.md` and `CLAUDE.md`; globals now render under a `## Global Instructions` section, matching the optimizer's existing `(global)` placement (#1088, closes #1072)
- `apm install` no longer masks local-bundle install failures with `UnboundLocalError`. (#1108)
- **`apm install <pkg>@<marketplace>` no longer fails for all marketplace packages.** The install resolver now accepts both legacy and current marketplace key names: `repository`/`repo` for github sources, `url`/`repo` for git-subdir sources, and `type`/`source` as the source-type discriminator. A scheme guard rejects full URLs passed through the `url` fallback. (#1106, closes #1105)
- **`apm install --update` no longer fails for GHES/generic hosts** that rely on git credential helpers (e.g., `git-credential-manager`) for authentication. The preflight auth probe was blocking credential helpers by setting `GIT_CONFIG_GLOBAL=/dev/null`; it now uses the same relaxed environment as the clone fallback path for non-GitHub/non-ADO hosts. (#1082)
- `apm compile --dry-run -t copilot` now faithfully simulates the hand-authored file guard: a `.github/copilot-instructions.md` lacking the APM marker is reported as `skipped=1` (matching the real run) instead of as `generated=1`. Previously dry-run would claim a write that a real run would refuse, giving CI preview gates a false signal. (#1048)
- `apm compile -t claude,copilot` (and any multi-target list including `copilot`) now correctly generates `.github/copilot-instructions.md`; previously it was silently skipped on the multi-target code path. (#1048)
- `apm compile` no longer overwrites a hand-authored `.github/copilot-instructions.md`; if the file lacks the APM-generated marker, regeneration is skipped with a warning that names the literal marker line (`<!-- Generated by APM CLI from .apm/ primitives -->`) so users can self-serve recovery. **Migration:** to adopt APM management of an existing file, either delete or rename it and re-run `apm compile`, or prepend the marker line to the top of the file and re-run `apm compile`. (#1048)
- Generated footer in `.github/copilot-instructions.md` now reads `apm compile` (was `specify apm compile`). (#1048)
- `apm compile --targets claude` no longer lists `@apm_modules/{owner}/{package}/CLAUDE.md` dependencies for packages that don't have a `CLAUDE.md` file on disk (#1047)
- **`apm prune` no longer flags directories it put there itself** -- skills installed from a subdirectory path (e.g., `owner/repo/.apm/skills/skill-name`) no longer cause the parent `owner/repo/` clone to appear as an orphan. Fixes spurious removal prompts in multi-skill and monorepo-style setups. The same fix applies to `apm deps list` and `apm compile`. A genuinely orphaned `owner/repo` package is still flagged even when a sibling subdirectory dep shares the same `owner/repo` root. No changes to `apm.yml` or the lockfile are required to benefit from this fix. (#1050)

## [0.11.0] - 2026-04-29

### Added

- **`apm pack` is now the single command for marketplace builds** -- with an `apm.yml` `marketplace:` block it emits `.claude-plugin/marketplace.json` directly. New flags: `--offline`, `--include-prerelease`, `--marketplace-output PATH`. (#722)
- **Author marketplaces from `apm.yml`.** New top-level `marketplace:` block, `apm marketplace migrate` to consolidate legacy `marketplace.yml`, `apm init --marketplace` to scaffold, and first-class `source: ./local/path` package sources. (#1038)
- **Codex CLI installs are now project-scoped.** MCP config lands in `.codex/config.toml` for project installs (no more polluting the user-global file); user-scope primitive deployment is also supported. (#803)
- **Cross-org private packages in `shared/apm.md`** via a new `apps:` array (one GitHub App per credential group, matrix fan-out). The single-app shorthand (`app-id` / `private-key` / `owner` / `repositories`) is preserved. (#982)

### Changed

- **`apm marketplace add` preserves the upstream alias** -- now defaults to the `name` declared in the fetched `marketplace.json` instead of the repo name, so install instructions in third-party READMEs work verbatim. (#1032)
- **`--policy` / `--policy-source` help is unified** across CLI and docs, with lockstep tests pinning all surfaces against drift. (#1000, closes #998 #994)
- **BREAKING: invalid `target:` values now fail loud.** CSV strings (`target: a,b,c`), unknown tokens, empty values, and `all` mixed with other targets used to silently no-op `apm install` / `apm compile`; they now raise a parse error. Omitting `target:` still auto-detects. (#820)
- Rename `DownloadStrategyManager` -> `DownloadDelegate` and fix double-checked-locking bug in marketplace registry `_load()`. (#918)

### Removed

- **BREAKING: `apm marketplace build` removed** -- `apm pack` is the replacement; the old verb exits 2 with a migration hint. The `marketplace_authoring` experimental flag is also gone (authoring is GA). (#722)

### Deprecated

- Standalone `marketplace.yml` -- still loaded with a deprecation warning, removal slated for v0.13. (#1038)

### Fixed

- **`apm install` works with your existing credential chain (SSO, EMU, GHES tokens).** Validation now uses the same credentials as the actual clone (PAT header-injected, then git credential helper, then SSH for explicit `#ref` pins) -- enterprise users whose env-var PAT has narrower SSO/EMU access than their `gh auth setup-git` / OS keychain are no longer false-rejected by the installer's API probe. Validation logic is now a separate module (`github_downloader_validation.py`), laying groundwork for future credential-provider extensibility. (#941)
- **`shared/apm.md` single-credential-group runs no longer fail validation** with a spurious `missing APM bundles: apm-default` -- a normalisation step recreates the per-group subdir layout that `actions/download-artifact@v5+` flattens away. (#1051)
- **`apm pack` works against GitHub Enterprise and other Git hosts** -- honors `GITHUB_HOST` for GHES auth and accepts GitHub / GHES / GitLab / Bitbucket / ADO / SSH URL forms. (#1008)
- **ADO Entra ID auth no longer silently fails.** Bearer tokens from `az account get-access-token` are plumbed through, errors are typed + actionable (4-case diagnostic), and `apm install --update` pre-flights auth before touching files. (#1015)
- `apm marketplace add` now uses the `name` field from the fetched `marketplace.json` as the default local alias, falling back to the repo name only when the manifest omits it or declares an invalid value. This restores parity with Claude Code install instructions (e.g. `addyosmani/agent-skills` registers as `addy-agent-skills` as that repo's README documents). Existing marketplace entries are unaffected; use `--name` to override explicitly. (#1032)
- `GEMINI.md` is now only created when explicitly targeted. (#1019)
- Windows-friendly: auto-discovery CLI output uses POSIX paths so `apm install` / `apm compile` output is readable on Windows. (#1018)
- Generated-file footer no longer prints stray `specify` before `apm compile`. (#996)
- CodeQL `clear-text-storage` false-positive resolved (variable rename). (#1002)

## [0.10.0] - 2026-04-27

### Added

- **Microsoft 365 Copilot Cowork** target works end-to-end: `apm install --target cowork --global` deploys skills to OneDrive (behind `apm experimental enable cowork`). (#926)
- **[Experimental] `apm marketplace` authoring CLI**: maintainers can scaffold, build, validate, and publish Anthropic-compliant marketplaces from the CLI (`init` -> `package add` -> `build` -> `publish`). (#790)
- README "Coming from `npx skills add`?" 30-second migration table for users arriving from the agentskills.io ecosystem. (#980)
- `NullCommandLogger` class (`src/apm_cli/core/null_logger.py`) -- null-object pattern for logger injection, eliminating 32 conditional logger forks in `MCPIntegrator`. (#918)
- Thread-safety infrastructure: `_get_console()` double-checked locking singleton, marketplace registry cache `threading.Lock`. (#918)
- 40 characterisation tests for `MCPIntegrator` methods (`install()`, `remove_stale()`, `collect_transitive()`). (#918)
- `_build_children_index()` helper in uninstall engine for O(n) reverse-dependency lookups. (#918)
- Performance benchmarks and scaling guards for complexity audit refactors (`tests/benchmarks/test_audit_benchmarks.py`, `test_scaling_guards.py`): 16 benchmark tests covering dependency parsing, children index, primitive discovery, registry cache, console singleton, and NullCommandLogger; 3 scaling-ratio guards run in the default test suite to catch O(n^2) regressions. (#918)
- Expanded performance benchmark suite with P0 and P1 hot-path coverage: `compute_package_hash`, `get_all_dependencies`, `is_semantically_equivalent`, `flatten_dependencies`, `to_yaml`, `compute_deployed_hashes`, `optimize_instruction_placement`, `_rewrite_markdown_links`, `partition_managed_files`, LockFile round-trip, and `register_contexts` -- 52 new benchmark tests plus 2 additional scaling guards. (#918)
- Iteration 2 benchmark coverage: `_match_double_star` recursive glob matcher, `ContentScanner.scan_text` and `strip_dangerous` security scanning, `build_dependency_tree` BFS resolver, `_parse_ls_remote_output` and `_sort_remote_refs` git ref parsing, `analyze_directory_structure` compiler analysis, and `collect_transitive` MCP integration -- 77 new benchmark tests plus 1 additional scaling guard. (#918)

### Changed

- [Experimental] `apm marketplace plugin` renamed to `apm marketplace package` (npm/pip/cargo familiarity); `--help` grouped into Consumer / Authoring sections. (#722)
- `MCPIntegrator` logger handling: methods default to `NullCommandLogger` instead of `None`, removing 32 `if logger:` / `elif logger:` conditional forks (net -91 production lines). (#918)
- Install pipeline lockfile reads reduced from 2x to 1x by caching early lockfile on `InstallContext`. (#918)
- `APMPackage.from_apm_yml()`: deduplicated dependency parsing via `_parse_dependency_dict()` classmethod. (#918)
- Uninstall engine BFS orphan detection: O(n^2) full-scan replaced with O(n) reverse-dep index. (#918)
- Primitive discovery scanning: 9+ `glob.glob()` calls replaced with single `os.walk` + `fnmatch` pass. (#918)
- MCP registry config reads: O(servers x runtimes) reduced to O(runtimes) via function-scoped cache. (#918)
- `_get_console()`: returns thread-safe singleton instead of creating new `Console()` per call. (#918)
- Marketplace registry cache: `_load()`, `_save()`, `_invalidate_cache()` protected with `threading.Lock`. (#918)
- Complexity audit -- decomposed god functions in `reference.py`, `audit.py`, `deps/cli.py`, and `script_runner.py` into focused single-responsibility helpers (largest: `audit()` 290 lines split into thin dispatcher + `_audit_ci_gate` + `_audit_content_scan` with shared `_AuditConfig` dataclass). (#918)
- Decomposed `github_downloader.py` into three modules: `git_remote_ops.py` (ref parsing), `download_strategies.py` (backend downloads), and a slimmed orchestrator. (#918)
- Decomposed `install()` god function (555 lines) into focused helpers with `InstallContext` parameter bundle. (#918)

### Fixed

- Docs site auto-deploys again after bot-cut releases (now triggered on tag push). (#981)
- Bare `except:` clauses in `formatters.py` (5) and `script_formatters.py` (2) now catch `Exception` instead of `BaseException`, allowing `KeyboardInterrupt` and `SystemExit` to propagate correctly. (#918)
- Silent auth fallback in `discovery.py:_get_token_for_host()` now logs `logger.debug()` when the token manager fails, making credential resolution failures visible with `--verbose`. (#918)
- Silent `except Exception: pass` handlers in `agents_compiler.py` (3) now emit `_logger.debug()` traces for config loading and constitution injection failures. (#918)
- Double `iterdir()` walk in `script_runner.py:_resolve_prompt_file()` collapsed to a single pass. (#918)

### Documentation

- Clarify `NullCommandLogger` partial interface and visible-output semantics in docstring. (#918)

### Maintainer tooling

- `pr-description-skill` ships an evals suite so PR-description quality regressions are caught in CI without an LLM API key. (#985)
- `pr-description-skill` mermaid guidance hardened with `assets/mermaid-conventions.md` (diagram-type-by-intent + GitHub-renderer gotchas `mmdc` misses). (#984)
- Cowork tests mock `sys.platform` so the macOS auto-detection paths don't false-fail on Windows CI. (#989)

## [0.9.4] - 2026-04-27

### Added

- **Day-0 install parity with `npx skills add`**: every public repo that installs cleanly with `npx skills add owner/repo` now installs with `apm install owner/repo`. APM recognises bare `skills/<name>/SKILL.md` (vercel-labs/agent-skills, xixu-me/skills, larksuite/cli, the agentskills.io ecosystem) as a first-class shape (`SKILL_BUNDLE`); `apm.yml` is optional. `--skill <NAME>` (repeatable) selects a subset and **persists** it to `apm.yml` + `apm.lock.yaml`, so bare `apm install` is reproducible across machines. `--skill '*'` resets; `apm audit --ci` flags drift. (#974)
- `curl | sh` install works in air-gapped, GHE, and internal-mirror setups: `install.sh` now reads `APM_INSTALL_DIR`, `GITHUB_URL`, `APM_REPO`, and `VERSION` (or `@vX.Y.Z` arg) -- pinning a version skips the GitHub API entirely, so corporate runners without api.github.com egress can bootstrap APM. (#660)

### Changed

- `apm marketplace` authoring commands (init, build, check, outdated, doctor, publish, package) ring-fenced behind `apm experimental enable marketplace-authoring` feature flag (default: disabled) (#790)
### Fixed

- `apm install` no longer fails behind corporate TLS-intercepting proxies: validation now honours `REQUESTS_CA_BUNDLE` instead of misreporting CA failures as auth errors. (#911)
- `apm experimental <subcommand> --help` now shows the subcommand's own help; CLI help text and short flags aligned from the 2026-04-24 audit. (#910, #903)

### Maintainer tooling

- Untriaged issues are auto-triaged by the `apm-triage-panel` skill on a daily oldest-first sweep (max 10/run) plus `status/needs-triage` for on-demand. Decisions are agentic proposals; any maintainer label edit wins. (#954)
- Docs site auto-deploys again after bot-cut releases (workaround for GitHub's `GITHUB_TOKEN`-suppressed `release: published` event). Silently broken since v0.9.3. (#953)
- Triage-panel themed issues now reach the PGS project board (dispatches `project-sync` directly; same `GITHUB_TOKEN` event-suppression class as #953). (#971)
- Issue templates use canonical APM labels; `python-architect` agent documents the mermaid `classDiagram :::cssClass` GitHub-render trap. (#958, #970)

## [0.9.3] - 2026-04-26

### Added

- **Gemini CLI** as a supported APM target (`--target gemini`): auto-detects `.gemini/`, writes MCP config to `.gemini/settings.json`, and adds `apm runtime setup|remove gemini`. (#917)
- Experimental `cowork` target for Microsoft 365 Copilot Cowork custom-skill deployment via OneDrive (`apm experimental enable cowork`; `apm install --target cowork --global`; persisted via `apm config set cowork-skills-dir`). (#913)
- `apm experimental` command group (`list` / `enable` / `disable` / `reset`) lets you opt into new behaviour before it graduates to default. Ships with the `verbose-version` flag. (#849)
- `apm audit --ci` now verifies hash integrity of locally deployed `.apm/` files so hand-edits and config drift fail CI instead of slipping through. (#887)
- `includes:` manifest field (`auto` or list) gives you explicit control over which local `.apm/` files are deployed; pair with `policy.manifest.require_explicit_includes` to block silent expansion. Audit raises an `includes-consent` advisory while you migrate. (#887)
- `apm-triage-panel` skill: three-persona issue triage panel (DevX UX, Supply Chain Security, APM CEO) emitting a single labelled-decision comment, mirroring `apm-review-panel`. (#915)
- `apm-primitives-architect` persona for designing and critiquing `.apm/` skill bundles, plus a `pr-description-skill` that enforces self-sufficient PR bodies (TL;DR/Problem/Approach/Implementation/Diagrams/Trade-offs/Benefits/Validation/How-to-test) with anchored citations and validated mermaid. (#882, #884)
- New docs guide [`dev-only-primitives`](https://danielmeppiel.github.io/awd-cli/guides/dev-only-primitives/): canonical pattern for maintainer-only primitives that must not ride into your published bundle. (#949)
- Maintainer tooling: PGS project-board sync workflow keeps issues in lockstep with labels/milestones; `APM Self-Check` CI job dogfoods `apm audit --ci` and regeneration-drift gates. (#919, #885)

### Changed

- HYBRID-skill review pipeline: `apm-review-panel` now produces a single CEO-synthesized verdict per run (no per-persona spam), with Hybrid E auth-expert routing and `python-architect`'s mandatory three-artifact contract. PRs get one high-signal comment. (#882, #905, #907, #908)
- Faster primitive discovery on large repos: `compilation.exclude` patterns now prune traversal at the directory level instead of post-filtering. (#870)
- `apm-action` bumped to `v1.4.2` (used by `shared/apm.md` workflows): fixes restore-mode workspace pollution that was overwriting your tracked `apm.lock.yaml` / `apm.yml` / `apm_modules`. (#904)
- CI release-binary smoke (Linux x64/arm64, Windows) only runs on tag/schedule/dispatch instead of every push, cutting ~15 redundant codex downloads/day; release-time gating unchanged. (#878)
- Branch-protection docs: clarify the required check-run name is `gate` (not the workflow display string `Merge Gate / gate`). (#874)

### Fixed

- HYBRID packages (apm.yml + SKILL.md, no `.apm/`) and CLAUDE_SKILL packages with sibling `agents/`/`assets/`/`scripts/` dirs now install correctly via the skill-bundle path; previously `apm install` rejected them silently and looked like a hang. Direct-dependency integration failures now print `[x]` and exit 1 instead of failing silently. (#946)
- `apm update` no longer breaks on Debian trixie arm64, Fedora 43, and similar distros where the bundled PyInstaller `LD_LIBRARY_PATH` was leaking into system `curl`/`tar` and triggering `libssl.so.3: version 'OPENSSL_3.2.0' not found`. Closes #894 (#899)
- `apm install` at user scope no longer recursive-globs your entire home directory: scan is scoped to `~/.apm/`. Fixes #830 (#850)
- `apm audit --ci` now also catches drift / missing / tampered files for locally-authored `.apm/` content (not just installed packages); `apm pack` strips local-content fields so they can't leak into bundled lockfiles. (#887)
- `apm install` for ADO orgs that disable PAT creation: error messages on generic git hosts now surface the custom port too. Custom transport types in MCP-server config are validated up front (defaulting to `http` when missing) instead of writing garbage. Closes #791 (#812)
- Windows: `apm install` no longer false-positives `PathTraversalError` on policy cache dirs that don't yet exist (8.3 short-name resolution mismatch). (#895)
- macOS: long inline policy YAML strings (>1023 bytes) no longer crash with `OSError [Errno 63] File name too long`; they fall back to string-mode parsing. Closes #848 (#860)
- Merge queue: `gate` check now reports inside the queue (added `merge_group` trigger), unblocking PRs that were stuck on "Expected -- Waiting for status to be reported". (#921)
- `apm-review-panel` workflow only runs on PRs labelled `panel-review`, eliminating spurious panel runs on every PR. (#948)

### Removed

- Deleted dead `ci-integration-pr-stub.yml` workflow stubs left over from the pre-merge-gate model. No user impact; reduces CI noise. (#875)

## [0.9.2] - 2026-04-23

### Added

- `apm install` supports Azure DevOps AAD bearer-token auth via `az account get-access-token`, with PAT-first fallback for orgs that disable PAT creation. Closes #852 (#856)
- New `enterprise/governance-guide.md`: flagship governance reference for CISO / VPE / Platform Tech Lead audiences; trims duplication across `governance.md`, `apm-policy.md`, `integrations/github-rulesets.md`; adds `templates/apm-policy-starter.yml`. (#851)
- Enterprise docs IA refactor: hub page + merged team guides, deduped governance content. (#858)
- Landing page rewritten around the three-pillar spine. (#855)
- First-package tutorial rewritten end-to-end; fixes `.apm/` anatomy hallucinations. (#866)
- `apm install --ssh` / `--https` flags and `APM_GIT_PROTOCOL=ssh|https` env to pick the initial transport for shorthand dependencies (#778)
- `apm install --allow-protocol-fallback` flag and `APM_ALLOW_PROTOCOL_FALLBACK=1` env as the migration escape hatch for cross-protocol fallback (#778)
- Add APM Review Panel skill (`.github/skills/apm-review-panel/`) and four new specialist personas (`devx-ux-expert`, `supply-chain-security-expert`, `apm-ceo`, `oss-growth-hacker`) with auto-activating per-persona skills. Routes specialist findings through an APM CEO arbiter for strategic / breaking-change calls, with the OSS growth hacker side-channeling adoption insights via `WIP/growth-strategy.md`. Instrumentation per Handbook Ch. 9 (`The Instrumented Codebase`); PROSE-compliant (thin SKILL.md routers, persona detail lazy-loaded via markdown links, explicit boundaries per persona).
- `apm view plugin@marketplace` displays marketplace plugin metadata (name, version, source, description) (#514)
- `apm outdated` checks marketplace plugin refs and shows a "Source" column distinguishing marketplace vs git updates (#514)
- `apm marketplace validate` command with schema validation and duplicate name detection (#514)
- Ref immutability advisory: caches plugin-to-ref pins and warns when a previously pinned plugin's ref changes (#514)
- Multi-marketplace shadow detection: warns when the same plugin name appears in multiple registered marketplaces (#514)

### Changed

- gh-aw workflows now use `imports:` for shared APM context instead of the deprecated `dependencies:` field. (#864)
- CI: `merge-gate.yml` orchestrator turns dropped `pull_request` webhook deliveries into clear red checks instead of stuck `Expected -- Waiting for status to be reported`. (#865)
- CI: `Merge Gate / gate` aggregates all PR-time required checks (`Build & Test (Linux)` + 4 stubs) into a single verdict; branch protection requires only this one check, decoupling the ruleset from CI workflow topology (Tide / bors pattern). (#867, #868)
- CI: `merge-gate.yml` simplified to a single `pull_request` trigger with `workflow_dispatch` for manual recovery; the dual-trigger redundancy attempt was poisoning the branch-protection rollup with `CANCELLED` check-runs. (#868)

### Fixed

- `apm install` surfaces the custom port in clone / `ls-remote` error messages for generic git hosts. (#804)

## [0.9.1] - 2026-04-22

### Added

- `apm install` enforces org `apm-policy.yml` at install time (deps deny/allow/require, MCP deny/transport/trust-transitive, `compilation.target.allow`, `extends:` chains, `policy.fetch_failure` knob, `policy.hash` pin); `--no-policy` / `APM_POLICY_DISABLE=1` escape hatch; `--dry-run` previews verdicts; failed package installs roll back `apm.yml`. New `apm policy status` diagnostic (table / `--json`, exit-0 by default, `--check` for CI). `apm audit --ci` auto-discovers org policy. **Migration**: orgs publishing `enforcement: block` may see installs that previously succeeded now fail -- preview with `apm install --dry-run`. Closes #827, #829, #831, #834 (#832)
- `pr-review-panel` gh-aw workflow: runs the `apm-review-panel` skill on PRs labelled `panel-review` and posts a synthesized verdict (#824)

### Changed

- Docs site publishes only on stable APM releases, not on every push to `main`. Closes #641 (#822)
- Dogfood APM: authored skills, agents, and instructions live in `.apm/`; `.github/{skills,agents,instructions}/` are regenerated by `apm install --target copilot` and committed (#823)

### Fixed

- `pr-review-panel` workflow now runs on PRs from forks: switched to `pull_request_target` with label-only triggering and a workflow-dispatch path (#826, #836, #837)
- Lowercase the host axis of the `_fallback_port_warned` dedup key so deps that differ only in hostname casing collapse to one cross-protocol fallback warning, matching the `AuthResolver._cache` convention (RFC 4343). Closes #800 (#815)

## [0.9.0] - 2026-04-21

### Changed (BREAKING)

- Strict-by-default git transport selection: explicit `ssh://`/`https://` URLs no longer silently cross-fall back; shorthand defaults to HTTPS (consults `url.<base>.insteadOf`). Opt back into the legacy chain with `--allow-protocol-fallback` or `APM_ALLOW_PROTOCOL_FALLBACK=1`. Adds `--ssh` / `--https` / `APM_GIT_PROTOCOL` for explicit shorthand selection. Closes #328 (#778)
- MCP entry validation hardened: names must match `^[a-zA-Z0-9@_][a-zA-Z0-9._@/:=-]{0,127}$`, URLs limited to `http`/`https`, headers reject CR/LF, stdio commands reject `..`. Error messages now include a valid positive example. (#807)
- Stdio MCP entries with whitespace in `command` and no `args` are rejected at parse time with a fix-it error pointing at the canonical `command: <binary>, args: [...]` shape. Closes #806 (#809)

### Added

- `apm install --mcp NAME` (and `apm mcp install` alias) for declaratively adding MCP servers to `apm.yml`, with `--transport` / `--url` / `--env` / `--header` / `--mcp-version` / `--registry` flags and stdio passthrough. TTY prompts on replace, `--force` required in CI. Includes `--registry URL` and `MCP_REGISTRY_URL` env for custom (enterprise) MCP registries. Closes #807 (#810)
- HTTP dependency support via `--allow-insecure` + `allow_insecure: true` dual opt-in; `--allow-insecure-host` for transitive HTTP from new hosts; credential-helper suppression on HTTP attempts to prevent token leakage; new `apm deps list --insecure` view with `Origin` column. Threat model in `enterprise/security.md`. Thanks @arika0093! (#700)
- Multi-target support: `apm.yml` `target` accepts a list (`[claude, copilot]`) and CLI `--target` accepts comma-separated values; only specified targets are compiled/installed/packed. Single-string form remains backward compatible. (#628)
- Marketplace UX overhaul: `apm view plugin@marketplace`, `apm outdated` Source column, `apm marketplace validate`, ref-immutability advisory, multi-marketplace shadow detection. (#514)
- New **MCP Servers** guide (`docs/guides/mcp-servers.md`) consolidating stdio / registry / remote shapes, flag reference, validation rules, and the conflict matrix in one page; assorted MCP doc drift fixes (#808)
- Build-time `update_policy` module so package-manager distributions (conda-forge, brew, pixi) can disable `apm update` and show custom guidance. Thanks @joostsijm! (#675)
- APM Review Panel skill (`.github/skills/apm-review-panel/`) plus four specialist personas (devx-ux, supply-chain-security, apm-ceo, oss-growth-hacker) routing through an APM CEO arbiter (#777)

### Fixed

- Preserve custom git ports across protocols: non-default ports on `ssh://` / `https://` dependency URLs (e.g. Bitbucket Datacenter SSH 7999, self-hosted GitLab HTTPS 8443) are captured as `DependencyReference.port` and reused on HTTPS fallback. Closes #661, #731 (#665)
- Token resolution now discriminates by port, fixing credential collisions across multiple self-hosted Git instances on the same host. Thanks @edenfunf! Closes #785 (#788)
- Detect port-like first path segment in SCP shorthand (`git@host:7999/path`) and raise an actionable error suggesting the `ssh://` form. Closes #784 (#787)
- `--allow-protocol-fallback` emits a one-shot `[!]` warning when a dependency's custom port is about to be tried across both SSH and HTTPS, recommending pinning the scheme. Closes #786 (#789)
- `apm install --global` now installs MCP servers to global-capable runtimes (Copilot CLI, Codex CLI) instead of skipping all MCP at user scope; `--trust-transitive-mcp` no longer ignored under `--global`. Lockfile-path behavior at `--global` tracked in #794 (#638)
- `apm install` no longer silently drops skills/agents/commands when a Claude Code plugin also ships `hooks/*.json`: detection cascade now classifies plugin-shaped packages as `MARKETPLACE_PLUGIN` first; emits a `[!]` warning when a hook-only classification disagrees with package contents (#780)
- `apm mcp search` / `list` / `show` now honour `MCP_REGISTRY_URL` (previously hardcoded to the public registry), print a `Registry: <url>` diagnostic when set, and surface the configured URL in network-error messages (#813)
- VS Code adapter defaults to `http` transport when `transport_type` is missing from remote registry data, matching Copilot adapter behavior (#654)
- `apm init` no longer prompts to overwrite three times on Windows CP950 terminals. Closes #602 (#647)
- `apm init` Next Steps panel surfaces install/marketplace/plugin workflows instead of the dead-end `apm run start` reference. Closes #603 (#649)

### Security

- `MCP_REGISTRY_URL` validated at startup (schemeless / unsupported schemes rejected; `http://` rejected by default, opt in via `MCP_REGISTRY_ALLOW_HTTP=1`); APM fails closed when a custom registry is unreachable during install pre-flight, instead of silently approving every MCP dep. Default registry keeps assume-valid for transient errors. (#814)
- `apm install --mcp` defense-in-depth: rejects embedded `..` in dep names with a valid positive example, redacts URL credentials in diagnostic output (`https://user:token@host/` -> `https://host/`), warns on `--registry` / `MCP_REGISTRY_URL` pointing at loopback / link-local / RFC1918 / cloud-metadata hosts (including decimal-encoded loopback). (#810)
- `SimpleRegistryClient` applies a `(connect=10s, read=30s)` timeout on every registry HTTP call, removing the unbounded-hang failure mode. Tunable via `MCP_REGISTRY_CONNECT_TIMEOUT` / `MCP_REGISTRY_READ_TIMEOUT`. (#810)

## [0.8.12] - 2026-04-19

### Added

- `apm install` now automatically discovers and deploys local `.apm/` primitives (skills, instructions, agents, prompts, hooks, commands) to target directories, with local content taking priority over dependencies on collision (#626, #644)
- Deploy primitives from the project root's own `.apm/` directory alongside declared dependencies, so single-package projects no longer need a sub-package stub to install their own content (#715)
- Add `temp-dir` configuration key (`apm config set temp-dir PATH`) to override the system temporary directory, resolving `[WinError 5] Access is denied` in corporate Windows environments (#629)

### Changed

- Refactor `apm install` into a modular engine package (`apm_cli/install/`) with discrete phases and apply Strategy / Template Method / Application Service patterns; public CLI behaviour and the `#762` cleanup chokepoint unchanged (#764)
- `apm marketplace browse/search/add/update` now route through the registry proxy when `PROXY_REGISTRY_URL` is set; `PROXY_REGISTRY_ONLY=1` blocks direct GitHub API calls (#506, #617)
- CI: adopt GitHub Merge Queue with tiered CI (Tier 1 unit + binary on `pull_request` + `merge_group`; Tier 2 integration + release-validation on `merge_group` only) plus an inert `pull_request_target` stub workflow for required-check satisfaction. CODEOWNERS now requires Lead Maintainer review for any change to `.github/workflows/**` (#770, #771)
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
