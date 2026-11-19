---
applyTo: "CHANGELOG.md"
description: "Changelog format and conventions based on Keep a Changelog"
---

# Changelog Format

This project follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) and [Semantic Versioning](https://semver.org/).

## Structure

- New entries go under `## [Unreleased]`.
- Released versions use `## [X.Y.Z] - YYYY-MM-DD`.
- Each version may open with `### Highlights` (2-4 bullets) and, when users must act, `### Breaking changes and upgrade notes` (one line per change, with the migration).
- Then group entries by type: `Added`, `Changed`, `Deprecated`, `Removed`, `Fixed`, `Security`.

## Entry format

- One line per user-visible change: the user-visible effect first, 25 words or fewer, ending with `(#PR_NUMBER)`. No implementation detail; that belongs in the PR.
- Combine related PRs into one line: `(#251, #256, #258)`. Use `(closes #N, #PR)` when the PR closes an issue.
- Credit external contributors inline: `-- by @username (#PR_NUMBER)`.
- Breaking changes go under `### Breaking changes and upgrade notes`, prefixed `**BREAKING:**`, with a one-line migration in the same line.
- Use backticks for code references: commands, file names, config keys, classes.
- Keep caveats that matter to users (for example, an unverified environment). Describe security hardening neutrally.
- ASCII only.

## Rules

- Before adding a line, search `[Unreleased]` for your PR number. If a line already describes your change, edit that line; never add a second line for the same change. Only a PR with several unrelated user-visible fixes gets one line per fix.
- Every merged PR with a user-visible effect gets an entry. Internal-only PRs (CI, tests, refactors, maintainer skills) get none.
- Do NOT include version-bump or release-machinery PRs (e.g., "chore: bump to vX.Y.Z").
- When releasing, move Unreleased entries into a new versioned section; never delete them. `cut-release` re-sanitizes the block into this shape.

## Examples

Good:

- `apm uninstall` keeps a surviving package's `skills:` selection instead of redeploying all its skills. (#3784)
- Repository names ending in `g`, `i`, `t` or `.`, such as `git/git`, install again. -- by @goranai (#3767)

Bad:

- Org policy `mcp.transport.allow` now checks the transport APM actually writes: registry MCP servers are looked up in their registry and checked on what install would write (a package counts as `stdio`, ...) ... (#3788)
  -- a paragraph of implementation detail. Write: "`mcp.transport.allow` checks registry MCP servers on what install writes. (#3788)" and put the policy impact in the upgrade notes.
- The MCP retired-shape heal lifecycle test now asserts the 0.37 `mcp.transport.allow` contract. (#3877)
  -- test-only change; no entry.
