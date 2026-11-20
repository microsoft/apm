# Migrate, merge or retire existing lifecycle tests

Load ONLY when moving, merging or retiring an existing
`*_lifecycle*.py` file (or a test inside one). Writing a new test does
not need this file.

## Before touching a file

1. List the node ids: `uv run pytest <file> --collect-only -q`.
2. For each test, write the promise it serves (SKILL.md Step 1). A test
   with no promise is unexplained; record it, do not delete it on that
   basis alone.
3. Classify each assertion: state oracle, output oracle (I6/I8/I9),
   or string-only. A test whose only assertion is a stdout substring is
   an anti-oracle candidate: replace it with a state oracle for the
   same promise, or flag it in the PR as not detecting the regression
   it appears to guard.

## When a test may go

A test may be removed only when one of these holds, shown with tool
output in the PR:

- It maps to no promise AND a maintainer agrees in the PR.
- A STRONGER test of the same cell strictly subsumes it: same promise,
  same cell (factors and side door), and stronger evidence (installed
  CLI vs engine, snapshot vs string, more invariants).
- It is an exact or equal copy of a kept twin: same body, fixtures,
  markers, conftest scope and CI lane.

Never remove a test because a heuristic says so (whole-file hashing,
a cut-candidate list, similar names). Heuristics nominate; the checks
above decide.

## Moving and merging

- Name the destination by promise or owner, never by phase, wave or
  gap.
- Keep the strongest markers: if any source test carries
  `lifecycle_smoke`, `e2e`, `requires_apm_binary`,
  `platform_sensitive` or `windows_compat`, the merged test keeps it.
- Keep parametrize ids stable where CI history or the bug ledger
  references them.
- Update every reference to the moved node id:
  `grep -rn '<old file>::<old test>' tests/ scripts/ .github/`.
  `tests/fixtures/lifecycle_bug_ledger.json` `regression_tests` and
  `known_gaps[].bounded_by` must point at real node ids;
  `uv run pytest tests/quality/test_lifecycle_bug_ledger.py -q` checks
  this.
- Moving into `tests/lifecycle/` (once it exists): convert the test to
  the target instrument (replay row, machine rule, scenario, table row
  or journey) so receipts are written by the oracle.

## Evidence for each wave

Every PR that moves, merges or retires lifecycle tests carries:

1. Node-set diff: collect-only before and after; every removed node is
   mapped to its kept twin or subsuming test.
2. Per-owner coverage-context diff: run the affected owners with
   `--cov --cov-context=test` before and after; no owner loses a line
   covered only by a removed test unless the PR says why.
3. Marker survival: the stronger markers listed above still select at
   least the same promises.
4. Targeted run of every touched module green with `-n 0`.
