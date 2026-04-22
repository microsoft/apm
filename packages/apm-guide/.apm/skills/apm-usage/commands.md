# CLI Command Reference

## Project setup

| Command | Purpose | Key flags |
|---------|---------|-----------|
| `apm init [NAME]` | Initialize a new APM project | `-y` skip prompts, `--plugin` authoring mode |

## Dependency management

| Command | Purpose | Key flags |
|---------|---------|-----------|
| `apm install [PKGS...]` | Install packages | `--update` refresh refs, `--force` overwrite, `--dry-run`, `--verbose`, `--only [apm\|mcp]`, `--target` (comma-separated), `--dev`, `-g` global, `--trust-transitive-mcp`, `--parallel-downloads N`, `--allow-insecure`, `--allow-insecure-host HOSTNAME`, `--mcp NAME` add MCP entry, `--transport`, `--url`, `--env KEY=VAL`, `--header KEY=VAL`, `--mcp-version`, `--registry URL` custom MCP registry |
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
| `apm pack` | Bundle package for distribution | `-o PATH`, `-t TARGET`, `--archive`, `--dry-run`, `--format [apm\|plugin]`, `--force` |
| `apm unpack BUNDLE` | Extract a bundle | `-o PATH`, `--skip-verify`, `--force`, `--dry-run` |

## Marketplace

| Command | Purpose | Key flags |
|---------|---------|-----------|
| `apm marketplace add OWNER/REPO` | Register a marketplace | `-n NAME`, `-b BRANCH`, `--host HOST` |
| `apm marketplace list` | List registered marketplaces | -- |
| `apm marketplace browse NAME` | Browse marketplace packages | -- |
| `apm marketplace update [NAME]` | Update marketplace index | -- |
| `apm marketplace remove NAME` | Remove a marketplace | `-y` skip confirm |
| `apm marketplace validate NAME` | Validate marketplace manifest | `--check-refs`, `-v` |
| `apm search QUERY@MARKETPLACE` | Search marketplace | `--limit N` |
| `apm install NAME@MKT[#ref]` | Install from marketplace | Optional `#ref` override |
| `apm view NAME@MARKETPLACE` | View marketplace plugin info | -- |

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
| `apm runtime setup {copilot\|codex\|llm}` | Install a runtime | `--version`, `--vanilla` |
| `apm runtime list` | Show installed runtimes | -- |
| `apm runtime remove {copilot\|codex\|llm}` | Remove a runtime | `--yes` |
| `apm runtime status` | Show active runtime | -- |

## Experimental features

| Command | Purpose | Key flags |
|---------|---------|-----------|
| `apm experimental` | Default to `apm experimental list` | `-v` verbose |
| `apm experimental list` | List registered experimental flags or emit JSON for automation | `--enabled`, `--disabled`, `--json`, `-v` verbose |
| `apm experimental enable NAME` | Enable an opt-in experimental flag | `-v` verbose |
| `apm experimental disable NAME` | Disable an opt-in experimental flag | `-v` verbose |
| `apm experimental reset [NAME]` | Reset one flag or all flags to defaults; also cleans malformed overrides during bulk reset | `-y` skip confirm, `-v` verbose |

Experimental flags MUST NOT gate security-critical behaviour (content scanning, path validation, lockfile integrity, token handling, MCP trust, collision detection). Flags are ergonomic/UX toggles only.

## Configuration and updates

| Command | Purpose | Key flags |
|---------|---------|-----------|
| `apm config` | Show current configuration | -- |
| `apm config get [KEY]` | Get a config value (`auto-integrate`, `temp-dir`) | -- |
| `apm config set KEY VALUE` | Set a config value (`auto-integrate`, `temp-dir`) | -- |
| `apm update` | Update APM itself (or show distributor guidance when self-update is disabled at build time) | `--check` only check |
