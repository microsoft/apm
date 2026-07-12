# Release changelog community-fix coverage design

## Problem

The v0.25.0 release draft compressed PR #2155 into one broad changelog entry.
That removed the direct connection between the merged work and the 24 issues
that #2155 closed. It also hid distinct user-visible fixes behind architecture
language.

The release notes must let a community reporter answer two questions without
reading the pull request:

1. Was my issue fixed in this release?
2. What changed in the product?

## Scope

This correction changes the v0.25.0 section in `CHANGELOG.md` and synchronizes
the release PR description. It does not change runtime code, version numbers,
release workflows, or the post-merge tagging gate.

## Source of truth

GitHub PR #2155 `closingIssuesReferences` is the authoritative closure set:

`#2062`, `#2071`, `#2103`, `#2116`, `#2126`, `#2127`, `#2128`, `#2129`,
`#2130`, `#2136`, `#2137`, `#2138`, `#2139`, `#2140`, `#2147`, `#2148`,
`#2149`, `#2150`, `#2156`, `#2157`, `#2158`, `#2159`, `#2160`, and `#2161`.

Issue titles and PR #2155's consolidated-fix sections provide the wording
evidence. Closed, unmerged point PRs are supporting history, not changelog
authorities.

## Changelog structure

Replace the broad #2155 summary with 11 user-outcome entries. Every entry names
the affected command or contract, explains the observable result, and ends with
`(closes #<issue>, ...; #2155)`. This keeps the merged PR distinct from the
issues it resolved.

### Changed

1. Canonical target acceptance and help: `#2138`, `#2147`.
2. Claude, Kiro, and Copilot hook contracts and provenance: `#2062`, `#2071`,
   `#2128`, `#2157`.

### Fixed

3. Missing plugin or skill selection and total install failure semantics:
   `#2103`, `#2116`, `#2126`.
4. Install rollback, resumability, and owned diagnostics: `#2129`, `#2140`,
   `#2161`.
5. Local-path MCP source and declaration drift detection: `#2127`, `#2136`.
6. Target contraction and legacy lockfile adoption: `#2139`, `#2149`, `#2158`.
7. Manifest and policy schema rejection: `#2137`.
8. Final compile orphan cleanup: `#2130`.
9. Shared-file ownership and atomic uninstall persistence: `#2148`, `#2160`.
10. Azure DevOps bearer preservation and stale-PAT fallback: `#2150`, `#2156`.

### Performance

11. Indexed deployment-ledger mutation: `#2159`.

## Data flow

1. Read #2155's closure set from GitHub.
2. Map every issue to exactly one user-outcome group.
3. Write the grouped changelog entries.
4. Extract all `closes` references attached to #2155 entries.
5. Compare the extracted set with GitHub's closure set.
6. Update the release PR body so its entry counts and validation claims remain
   truthful.
7. Run the release lint mirror, commit the correction, and push it to PR #2164.

## Error handling

- If the GitHub closure set changes before the correction is committed, stop
  and regenerate the mapping.
- If any issue is missing or duplicated, do not push.
- If an issue title and #2155's implementation summary disagree, use the
  user-observable behavior established by tests and flag the ambiguity.
- If the lint mirror fails, do not push the correction.

## Validation

The correction is complete when:

- The #2155 changelog entries contain exactly 24 unique `closes` references.
- That set equals PR #2155's `closingIssuesReferences`.
- Every #2155 entry explains a concrete user-visible behavior.
- The release PR body no longer claims a stale changelog-entry count.
- The release lint mirror passes.
- No release tag is created.

## Trade-offs

Eleven grouped entries are longer than one summary entry, but preserve
community traceability without repeating #2155 twenty-four times. One bullet
per issue was rejected because closely related issues describe one user-visible
fix and would make the release notes harder to scan.
