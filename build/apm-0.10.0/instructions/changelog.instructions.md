---
applyTo: "CHANGELOG.md"
description: "Changelog format and conventions based on Keep a Changelog"
---

# Changelog Format

This project follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) and [Semantic Versioning](https://semver.org/).

## Structure

- New entries go under `## [Unreleased]`.
- Released versions use `## [X.Y.Z] - YYYY-MM-DD`.
- Group entries by type: `Added`, `Changed`, `Deprecated`, `Removed`, `Fixed`, `Security`.

## Entry format

- One line per PR: concise description ending with `(#PR_NUMBER)`.
- Credit external contributors inline: `— by @username (#PR_NUMBER)`.
- Combine related PRs into a single line when they form one logical change: `(#251, #256, #258)`.
- Use backticks for code references: commands, file names, config keys, classes.

## Rules

- Every merged PR that changes code, tests, docs, or dependencies must have a changelog entry.
- Do NOT include version-bump or release-machinery PRs (e.g., "chore: bump to vX.Y.Z").
- When releasing, move Unreleased entries into a new versioned section — never delete them.
