# CLI Command Reference

## Project setup

| Command | Purpose | Key flags |
|---------|---------|-----------|
| `apm init [NAME]` | Initialize a new APM project | `-y` skip prompts, `--plugin` plugin authoring mode, `--marketplace` seed apm.yml with a `marketplace:` block |

## Dependency management

| Command | Purpose | Key flags |
|---------|---------|-----------|
| `apm install [PKGS...]` | Install APM and MCP dependencies (supports APM packages, Claude skills (SKILL.md), and plugin collections (plugin.json)) | `--update` refresh refs, `--force` overwrite, `--dry-run`, `--verbose`, `--only [apm\|mcp]`, `--target` (comma-separated; use `copilot-cowork` with `--global` after `apm experimental enable copilot-cowork`), `--dev`, `-g` global, `--trust-transitive-mcp`, `--parallel-downloads N`, `--allow-insecure`, `--allow-insecure-host HOSTNAME`, `--skill NAME` install named skill(s) from SKILL_BUNDLE (repeatable; persisted in apm.yml; `'*'` resets to all), `--mcp NAME` add MCP entry, `--transport`, `--url`, `--env KEY=VAL`, `--header KEY=VAL`, `--mcp-version`, `--registry URL` custom MCP registry |
| `apm uninstall PKGS...` | Remove packages | `--dry-run`, `-g` global |
| `apm prune` | Remove orphaned packages | `--dry-run` |
| `apm deps list` | List installed packages | `-g` global, `--all` both scopes, `--insecure` |
| `apm deps tree` | Show dependency tree | -- |
| `apm view PKG [FIELD]` | View package details or remote refs | `-g` global, `FIELD=versions` |
| `apm outdated` | Check locked deps via SHA/semver comparison | `-g` global, `-v` verbose, `-j N` parallel checks |
| `apm deps info PKG` | Alias for `apm view PKG` local metadata | -- |
| `apm deps clean` | Clean dependency cache | `--dry-run`, `-y` skip confirm |
| `apm deps update [PKGS...]` | Update specific packages | `--verbose`, `--force`, `--target` (comma-separated), `--parallel-downloads N` |

## Compilation

| Command | Purpose | Key flags |
|---------|---------|-----------|
| `apm compile` | Compile agent context | `-o` output, `-t` target (comma-separated), `--chatmode`, `--dry-run`, `--no-links`, `--watch`, `--validate`, `--single-agents`, `-v` verbose, `--local-only`, `--clean`, `--with-constitution/--no-constitution` |

## Scripts

| Command | Purpose | Key flags |
|---------|---------|-----------|
| `apm run SCRIPT` | Execute a named script | `-p name=value` (repeatable) |
| `apm preview SCRIPT` | Preview script without running | `-p name=value` |
| `apm list` | List available scripts | -- |

## Security and audit

| Command | Purpose | Key flags |
|---------|---------|-----------|
| `apm audit [PKG]` | Scan for security issues | `--file PATH`, `--strip`, `--dry-run`, `-v`, `-f [text\|json\|sarif\|md]`, `-o PATH`, `--ci`, `--policy SOURCE`, `--no-cache`, `--no-fail-fast` |

## Distribution

| Command | Purpose | Key flags |
|---------|---------|-----------|
| `apm pack` | Build distributable artifacts (bundle and/or marketplace.json -- driven by `apm.yml`) | `-o PATH`, `-t TARGET`, `--archive`, `--dry-run`, `--format [apm\|plugin]`, `--force`, `--offline`, `--include-prerelease`, `--marketplace-output PATH` |
| `apm unpack BUNDLE` | Extract a bundle | `-o PATH`, `--skip-verify`, `--force`, `--dry-run` |

## Marketplace (consumer)

| Command | Purpose | Key flags |
|---------|---------|-----------|
| `apm marketplace add OWNER/REPO` | Register a marketplace | `-n NAME`, `-b BRANCH`, `--host HOST` |
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
| `apm marketplace outdated` | Report upgradable plugins, range-aware | `--offline`, `--include-prerelease`, `-v` |
| `apm marketplace check` | Validate the `marketplace:` block and verify refs resolve | `--offline`, `-v` |
| `apm marketplace doctor` | Diagnose git, network, auth, and marketplace config readiness | `-v` |
| `apm marketplace publish` | Open PRs on consumer repos from `consumer-targets.yml` | `--targets PATH`, `--dry-run`, `--no-pr`, `--draft`, `--allow-downgrade`, `--allow-ref-change`, `--parallel N`, `-y` |
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

Set `MCP_REGISTRY_URL` (default `https://api.mcp.github.com`) to point all `apm mcp` commands and `apm install --mcp` at a custom MCP registry. The URL is validated at startup and must use `https://`; set `MCP_REGISTRY_ALLOW_HTTP=1` to opt in to plaintext `http://` for development. When the override is set and the registry is unreachable during install pre-flight, APM fails closed.

## Runtime management (experimental)

| Command | Purpose | Key flags |
|---------|---------|-----------|
| `apm runtime setup {copilot\|codex\|llm\|gemini}` | Install a runtime | `--version`, `--vanilla` |
| `apm runtime list` | Show installed runtimes | -- |
| `apm runtime remove {copilot\|codex\|llm\|gemini}` | Remove a runtime | `-y`, `--yes` |
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

Experimental flags MUST NOT gate security-critical behaviour (content scanning, path validation, lockfile integrity, token handling, MCP trust, collision detection). Flags are ergonomic/UX toggles only.

## Configuration and updates

| Command | Purpose | Key flags |
|---------|---------|-----------|
| `apm config` | Show current configuration | -- |
| `apm config get [KEY]` | Get a config value (`auto-integrate`, `temp-dir`, `copilot-cowork-skills-dir`) | -- |
| `apm config set KEY VALUE` | Set a config value (`auto-integrate`, `temp-dir`; `copilot-cowork-skills-dir` requires `apm experimental enable copilot-cowork`) | -- |
| `apm config unset KEY` | Remove a stored config value (`temp-dir`, `copilot-cowork-skills-dir`) | -- |
| `apm update` | Update APM itself (or show distributor guidance when self-update is disabled at build time) | `--check` only check |

`apm config set copilot-cowork-skills-dir <absolute-path>` persists the Cowork skills directory across shells. `apm config get copilot-cowork-skills-dir` and `apm config unset copilot-cowork-skills-dir` remain available even when the `copilot-cowork` flag is disabled so leftover state can still be inspected or cleared. In `apm config` and bare `apm config get`, the `copilot-cowork-skills-dir` entry is shown only when the `copilot-cowork` flag is enabled.
