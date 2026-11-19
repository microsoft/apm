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

- One short line per user-visible change, ideally 25 words or fewer: what users can now do, or what no longer breaks. Details belong in the PR.
- End each line with `(#PR_NUMBER)`; combine related PRs into one line: `(#251, #256, #258)`.
- Credit external contributors inline: `-- by @username (#PR_NUMBER)`.
- Prefix breaking changes with `**BREAKING:**` in the upgrade-notes list.
- Use backticks for code references: commands, file names, config keys, classes.
- Keep caveats that matter to users (for example, an unverified environment). Describe security hardening neutrally.
- ASCII only.

## Rules

- Every merged PR with a user-visible effect gets an entry. Leave out internal-only PRs (CI, tests, refactors with no user effect).
- Do NOT include version-bump or release-machinery PRs (e.g., "chore: bump to vX.Y.Z").
- When releasing, move Unreleased entries into a new versioned section; never delete them.
