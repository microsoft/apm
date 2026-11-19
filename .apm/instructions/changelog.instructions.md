---
applyTo: "CHANGELOG.md"
description: "Changelog format and conventions based on Keep a Changelog"
---

# Changelog Format

This project follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) and [Semantic Versioning](https://semver.org/).

## One change, one line, one section

Each user-visible change appears exactly once in a version: one line, in one section. Never restate it in a second section.

- Breaking changes and changes users must act on (including deprecations that need action) go ONLY under `### Breaking changes and upgrade notes`, with the migration in the same line. Prefix true breakings with `**BREAKING:**`. Do not repeat them under `Added`, `Changed` or `Deprecated`.
- Everything else goes under exactly one type section: `Added`, `Changed`, `Deprecated` (no action needed yet), `Removed`, `Fixed`, `Security`.
- PRs never write `### Highlights`. `cut-release` adds 2-4 short Highlights pointers when it cuts the release.

## Structure

- New entries go under `## [Unreleased]`.
- Released versions use `## [X.Y.Z] - YYYY-MM-DD`.
- Section order: `Highlights` (release only), `Breaking changes and upgrade notes`, `Added`, `Changed`, `Deprecated`, `Removed`, `Fixed`, `Security`. Omit empty sections.

## Entry format

- User-visible effect first, 25 words or fewer, ending with `(#PR_NUMBER)`. No implementation detail; that belongs in the PR.
- Combine related PRs into one line: `(#251, #256, #258)`. Use `(closes #N, #PR)` when the PR closes an issue.
- Credit external contributors inline: `-- by @username (#PR_NUMBER)`.
- Use backticks for code references: commands, file names, config keys, classes.
- Keep caveats that matter to users (for example, an unverified environment) in the same line. Describe security hardening neutrally.
- ASCII only.

## Rules

- Before adding a line, search `[Unreleased]` for your PR number. If a line already describes your change, edit that line; never add another.
- A PR with several user-visible fixes groups them by theme into as few lines as stay clear (for example, one line for all `apm audit` fixes).
- Every merged PR with a user-visible effect gets an entry. Internal-only PRs (CI, tests, refactors, maintainer skills) get none.
- Do NOT include version-bump or release-machinery PRs (e.g., "chore: bump to vX.Y.Z").
- When releasing, move Unreleased entries into a new versioned section; never delete them. `cut-release` re-sanitizes the block into this shape.

## Examples

Good:

- `apm uninstall` keeps a surviving package's `skills:` selection instead of redeploying all its skills. (#3784)
- `mcp.transport.allow` checks the transport install actually writes; disallowed or unresolvable registry servers are now blocked, so set `transport:` or update the policy. (#3788)
  -- one line, only under upgrade notes, migration included.

Bad:

- The same change in three sections: a Highlights line "Org policy `mcp.transport.allow` checks the transport APM actually writes. (#3788)", an upgrade note about newly blocked servers (#3788), and a `Changed` line restating the check (#3788).
  -- keep only the upgrade-notes line above.
- The MCP retired-shape heal lifecycle test now asserts the 0.37 `mcp.transport.allow` contract. (#3877)
  -- test-only change; no entry.
