# semver-rubric -- pick the bump

Loaded by `cut-release` at Phase 2. Walks every PR enumerated in
Phase 1 against a fixed signal table; the highest-severity signal
wins.

## Signal table (highest severity wins)

| Signal (read top-down; first match wins) | Bump |
|------------------------------------------|------|
| 1. ANY PR title or [Unreleased] entry contains literal `**BREAKING:**` or `(BREAKING)` | **MINOR** (pre-1.0) / **MAJOR** (>= 1.0) |
| 2. ANY PR added a NEW user-visible CLI command (e.g. `apm deps why`, `apm publish`) | **MINOR** |
| 3. ANY PR added a NEW user-facing feature flag, config field, manifest field, or schema entry | **MINOR** |
| 4. ANY PR added a NEW normative spec, JSON Schema, or contract surface | **MINOR** |
| 5. ANY PR added a NEW dependency-resolution mode (e.g. semver ranges on git deps) | **MINOR** |
| 6. ANY PR added a NEW supported target / adapter / integration host | **MINOR** |
| 7. All remaining PRs are bug fixes, security patches, doc-only, internal | **PATCH** |
| 8. NO PRs since last tag | **REFUSE** -- nothing to release |

Read top to bottom. The first row whose condition matches is the
chosen bump. Do not "average" across rows.

## Pre-1.0 vs post-1.0 framing

This rubric is calibrated for the current 0.x line. Per the
project's stated semver discipline (`pyproject.toml` follows
semver, see `.github/instructions/changelog.instructions.md`):

- PRE-1.0 (0.x.y): BREAKING changes are allowed in a MINOR bump.
  This is the standard pre-1.0 semver convention. Mark the entry
  `**BREAKING:**` so users can grep for it, but do NOT bump to
  1.0 just because the cycle has a breaking change.
- POST-1.0 (>= 1.0): BREAKING requires MAJOR. Period.

## Major bump (escalate)

If the rubric would output >= 1.0.0 (the cycle contains a BREAKING
change AND the current version is already 0.99.x OR the operator
explicitly named a major bump), STOP and surface to the operator:

> The signals point to a major bump (>= 1.0.0). 1.0 is a
> positioning decision (API stability commitment), not a
> mechanical one. Confirm explicitly, or pick a different
> versioning strategy (e.g. ship as 0.X.Y+1 minor and defer 1.0
> to a planned release).

Do NOT bump to 1.0.0 without an explicit "yes, ship 1.0" from the
operator. Default to MINOR in the 0.x line even with breakings.

## Calculating the next version

Given current version `MAJOR.MINOR.PATCH`:

- PATCH bump: `MAJOR.MINOR.(PATCH+1)`.
- MINOR bump: `MAJOR.(MINOR+1).0`.
- MAJOR bump: `(MAJOR+1).0.0` (escalate first; see above).

Read the current version from `pyproject.toml` via
`grep '^version = ' pyproject.toml`. Do not recall it.

## Worked examples (calibration anchors)

### v0.16.0 cycle (the cycle that produced this skill)

Signals seen:
- One BREAKING in [Unreleased] (`apm install` exit-code change). -> Row 1.
- New command `apm deps why` (#1495). -> Row 2.
- New policy `require_pinned_constraint` (#1494). -> Row 3.
- New spec OpenAPM v0.1 (#1517). -> Row 4.
- New resolution mode: semver ranges on git deps (#1496). -> Row 5.

First match: Row 1 (BREAKING). Pre-1.0 framing -> **MINOR**.
Result: `0.15.0 -> 0.16.0`.

### Hypothetical patch-only cycle

Signals seen:
- Three PRs, all `fix(install): ...`. No BREAKING. No new
  command. No new schema.

First match: Row 7. -> **PATCH**.
Result: `0.16.0 -> 0.16.1`.

### Hypothetical no-op cycle

Signals seen:
- Five PRs, all touching `.apm/` skills, `.github/instructions/`,
  or `tests/`. All marked INTERNAL by Phase 1's script.

After dropping internals: 0 user-facing PRs.

First match: Row 8 (NO PRs). -> **REFUSE**. Surface to operator:
"Nothing user-facing has merged since v0.16.0. Release would be
no-op; recommend waiting for a real fix or feature."

## Output contract

Phase 2 must emit a structured summary to the operator before
checkpoint 1:

```
Next version: vX.Y.Z (BUMP from vA.B.C)
Signal: <row N> -- <one-line description>

Per-PR rationale:
  #1517 OpenAPM v0.1 spec       -> Row 4 (new spec)
  #1496 git-source semver       -> Row 5 (new resolution mode) + carries BREAKING (Row 1)
  #1495 apm deps why            -> Row 2 (new command)
  #1494 require_pinned_constraint -> Row 3 (new policy)
  ...
  #1486 windsurf uninstall fix  -> Row 7 (fix; subordinate to Row 1 above)

Why MINOR (not PATCH): Row 1 fired (BREAKING) AND Rows 2-5 fired.
Why MINOR (not MAJOR): pre-1.0 framing; breakings ride in minor.
```

This block is what the operator sees at checkpoint 1; it is also
what the PR body's "Why this bump" section quotes.
