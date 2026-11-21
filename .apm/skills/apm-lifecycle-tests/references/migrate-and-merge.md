# Moving, merging and retiring tests

Load ONLY before you move a test between directories, fold an old
`*_lifecycle.py` into the family, merge duplicates, or delete a test.

## Before touching a file

1. List node ids: `uv run --extra dev pytest <file> --collect-only -q`.
2. Name the promise for each test (SKILL.md Step 1).
3. Classify each assertion: state oracle, output oracle (I6/I8/I9) or
   string-only. String-only is an anti-oracle candidate.

## The rule

A test may be deleted only when ONE of these holds:

1. It maps to no promise after you searched for one. "No promise
   found" is often only "no attribution found", because many src
   modules have no owner entry. Search docs, `CHANGELOG.md` and
   `.apm/architecture/owners/` first, and get maintainer agreement.
2. A STRONGER test of the same cell strictly subsumes it: same input
   partition, same or stronger oracle, same or wider CI lane.
3. It is an EXACT or EQUAL copy of a test that stays.

Equal-copy checklist (all must hold):

- Same directory, or the same conftest chain.
- Identical function AST (docstrings ignored); module-level fixtures
  and constants identical too.
- The kept copy carries every marker the removed copy had (for example
  `lifecycle_smoke`, `platform_sensitive`, `windows_compat`).
- Any test unique to the removed file moves to the kept file first.
- If node ids change, update the duration seeds in
  `.github/ci-durations/*.json`.

Never remove a test because a heuristic says so (whole-file hashing, a
cut-candidate list, similar names). Heuristics nominate; the checks
above decide.

## Evidence for any removal or merge wave

- Selected node-set diff (collect-only before and after): only the
  intended nodes disappear; parametrized ids elsewhere are unchanged.
- Per-owner coverage-context diff (`--cov --cov-context=test` before
  and after): it must not drop.
- The list of kept twins or subsuming tests, by node id.
- Every reference to a moved node id updated:
  `grep -rn '<old file>::<old test>' tests/ scripts/ .github/`. The
  ledger (`regression_tests`, `known_gaps[].bounded_by`) is checked by
  `uv run --extra dev pytest tests/quality/test_lifecycle_bug_ledger.py -q`.

## Placement moves

- Follow fixtures and helpers before moving: a test can look pure
  while its fixture writes real config files.
- Behavioral class comes from the module-level marker (`unit`,
  `component`, `e2e`), not the directory. Pure `unit` modules belong in
  `tests/unit`; installed-CLI `e2e` modules in `tests/integration`.
- `tests/integration/test_architecture_*` modules are contract tests
  over repository source (`architecture_contract`); they stay where
  they are even though they spawn no process.

## Folding an old lifecycle file into the family

For each test, do exactly one of:

- Convert it to a replay row or scenario row (target family) or a
  parametrize case (today) with the SAME assertions, strengthening
  string-only assertions into state oracles.
- Keep it as a named regression trap and list it in the bug ledger.
- Merge it into an existing test as a parametrize case.

Never drop an assertion silently. If an assertion pins current wrong
behavior (an anti-oracle), fix it together with the src fix.

## Naming

Name files and tests after the promise or the owner. Never use phase,
wave, gap or coverage-pass names: those files are where copies
accumulate.
