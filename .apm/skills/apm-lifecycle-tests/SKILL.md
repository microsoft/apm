---
name: apm-lifecycle-tests
description: >-
  Use this skill when you write, extend or migrate an APM lifecycle
  test: any test that drives the real apm CLI through command
  sequences and checks durable state (deployed files, lockfile, MCP
  configs, approvals, egress). Apply it for a bug fix or behavior
  change in install, update, uninstall, prune, compile, audit,
  approve or deny, pack or bundle, marketplace, policy, MCP or LSP;
  for a regression trap; for a lifecycle_smoke test, state-machine
  rule, replay row, scenario, security table or docs journey; and
  whenever a reviewer says lifecycle coverage is missing, even if
  the request only says "add an e2e or integration test". It names
  the promise, picks the instrument, writes the failing-before test
  with snapshot and invariant oracles, and records the bug-ledger
  row. Not for pure unit tests, PR review verdicts, or deciding
  which tests to delete.
---

# APM lifecycle tests

A lifecycle test proves a USER PROMISE on the installed CLI. It drives
`apm` through real command sequences in a hermetic world, snapshots
durable state before and after, and asserts observable facts: files
present or absent, bytes unchanged, lockfile rows, MCP config entries,
egress. It is the only tier that can prove a negative ("nothing else
was written", "the user's file survived", "the denied hook is gone").
In agentic coding, where no human walks the CLI by hand, this harness
is the walkthrough.

Unit tests on each side of a boundary do not replace it. A lifecycle
test whose only assertion is a string compare is NOT lifecycle proof.

Surfaces that need it: install / uninstall / update / compile / audit
paths; the deployment ledger, lockfile write path or `deployed_files`;
target resolution, widening or narrowing; any write into a target root
(`.github/`, `.claude/`, `.cursor/`, `.agents/`, ...) or user scope
(`~/.apm/`); `--dry-run` on any command; cleanup, prune, drift or
orphan handling.

## Operating rules

- Single thread. Plan, execute, verify by running.
- Every fact is a tool result. "The test passes", "no test exists",
  "this path exists" come from `pytest`, `grep` or `ls` output in this
  session, never from memory or prose.
- Restate the promise id at the top of every step's notes.
- ASCII only in test code, ids, comments and output assertions.

## Procedure

### Step 0 -- locate the family

Run `ls tests/lifecycle/conftest.py`.

- Present: use the target family (world, driver, model, oracles,
  receipts, replay rows in `tests/lifecycle/replay/rows/*.yaml` run by
  `tests/lifecycle/rows.py`). Read the TARGET section of
  `references/family.md` and verify each name there with `ls`/`grep`.
- Absent (today): use the current harness: `ApmLifecycleRunner`,
  `IsolatedApmEnvironment`, `LifecycleStateSnapshot`,
  `LocalPackageFactory`, the `tests/integration/_*_kit.py` worlds, and
  the reference module
  `tests/integration/test_required_lifecycle_state_machine.py`. Read
  the TODAY section of `references/family.md`. This branch is
  authoritative until `tests/lifecycle/` exists.

### Step 1 -- name the promise, the cell and the owner

- PROMISE. A testable statement APM makes to a user, cited to source.
  Precedence: cross-cutting invariants I1-I9 (below); bug-ledger
  property ids (`property_catalog` in
  `tests/fixtures/lifecycle_bug_ledger.json`); OpenAPM `req-*` ids
  (`CONFORMANCE.json`); owner decisions
  (`.apm/architecture/owners/*.json`); `CHANGELOG.md` guarantees; docs
  claims with must / never / fails / exits wording. When
  `tests/lifecycle/` is present, rows use `P-INV-I1`..`P-INV-I9` and
  `P-LAW-<ledger property id>`; owner ids (`P-OWN-*`) resolve only
  once a promise catalog exists. Write:
  `PROMISE: <id> -- <sentence> (source: <path>)`.
- CELL. A factor tuple. Factors: OP, SRC, PRIM, SCOPE, LOCK, DEPLOYED,
  GATE, TGT, NET; modifiers FROZEN, DRYRUN, FORCE, SELECT, UPDATE,
  NOPOLICY. Example:
  `OP=update SRC=git PRIM=lsp SCOPE=project GATE=revoked-after-deploy`.
- OWNER. The module that decides the behavior (the `owner` field in
  `.apm/architecture/owners/*.json`). A fix belongs in the owner.

If you cannot name a promise, stop and ask. A test with no promise is
unexplained, not automatically deletable.

### Step 2 -- derive cells and check what exists

- Partition a factor only where the code branches; cite the branch
  (`file:line` from `grep -n`).
- install, update, `deps update` and `lock` share one pipeline. Each
  SIDE DOOR (a mutating path that skips the pipeline: local bundle,
  `--mcp`, the `--dry-run` planner, unpack) is its own family; defects
  cluster there.
- Pairwise (t=2) by default. t=3 only on split-authority triples:
  OP x GATE x PRIM, FROZEN x SCOPE x GATE, OP x SRC x PRIM,
  OP x SCOPE x LOCK, DRYRUN x OP x SRC.
- Equivalence is partial; name the exceptions (hosts same except lock
  identity; targets same except MCP ownership; file primitives same
  except compile staging).
- Search before writing: `grep -rn "<symbol|flag|error text|issue>" tests/`.
  Look in `tests/lifecycle/` (if present), then
  `tests/integration/*lifecycle*.py`. Prefer EXTENDING an existing
  test (parametrize case, assertion, replay row, machine rule) over a
  new module; lifecycle tests are expensive in CI time.

### Step 3 -- pick the instrument

| Situation | Today (no `tests/lifecycle/`) | Target family |
|---|---|---|
| one cell, fixed steps | `ApmLifecycleRunner` test or parametrize case in the matching `tests/integration/*lifecycle*.py` | replay row in `tests/lifecycle/replay/rows/*.yaml` |
| transition that must hold from many states | case in `test_required_lifecycle_state_machine.py` or rule in `test_generated_lifecycle_state_machine.py` | state-machine rule, where the family provides machines (verify with `ls`) |
| history, ownership of unmanaged assets, platform, failure phase, cache, graph or version, concurrency | scenario on a `_*_kit.py` world | planned scenario suite; today's harness until present |
| trust, policy outcomes, integrity, egress, origin, credentials | table-driven E2E with egress log or snapshot-absence oracle | planned security table; today's harness until present |
| docs golden path or user-visible transcript | real-CLI journey with every printed hint executed | planned journey step + golden; today's harness until present |
| flag combination the command must refuse | ONE refusal test (exit code + unchanged snapshot) | same |
| owner decision | table-driven test at the owner symbol (owner not patched) + one E2E | same |

A refusal test never discharges a success obligation. Derive
per-target parameters from `KNOWN_TARGETS`
(`src/apm_cli/integration/targets.py`); never hardcode target names.

### Step 4 -- write the failing-before test

Start from `assets/templates.md`. Mandatory properties:

- Hermetic world: isolated HOME and APM_HOME, local packages or local
  git remotes, local marketplace or MCP registry when needed. No
  network, no credentials.
- Real CLI (today): the packaged binary (`apm_binary_path`). The
  engine command (`apm_engine_command`, `python -m apm_cli.cli`) only
  for exploration or engine contracts. Never CliRunner for a lifecycle
  promise.
- Real CLI (when `tests/lifecycle/` is present): run every command
  through `driver.run(world, argv, scope=...)`; never choose the entry
  point yourself. `APM_LIFECYCLE_DRIVER` selects `engine` (PR default)
  or `binary` (nightly).
- Capture stdout, stderr and exit code separately.
- Snapshot `LifecycleStateSnapshot` before and after each mutating
  command; assert the exact changed set and that nothing else changed.
- Policy ON. Do not copy `--no-policy` from older helpers
  (`_INSTALL_ARGS` in the state-machine module carries it). Use it only
  when `--no-policy` is the behavior under test.
- Apply I1-I9 after every mutating step. Read `references/oracles.md`
  when writing the assertions.
- Module-level `pytestmark` with one behavioral marker (`e2e`) plus
  scheduling and prerequisite markers.

### Step 5 -- run red

```bash
APM_BINARY_PATH="$(uv run which apm)" \
  uv run --extra dev pytest <node> -n 0 -p no:cacheprovider -q
```

Add `APM_E2E_TESTS=1` when the module carries `requires_e2e_mode`. Use
a built binary instead of the in-tree one when one exists. Keep the
failing assertion line. A test that is green before the fix is not a
regression trap; tighten the oracle.

### Step 6 -- fix at the owner and guard it

- Route every caller through the owner. A fix at one call site of a
  decision made in several places is rejected in review.
- If a caller bypassed the owner, add or extend a CLOSED-WORLD guard
  in the architecture linter (it refuses callers not on its
  allowlist): register the rule id in `_EXPECTED_RULE_ID_TEXT`
  (`tests/unit/scripts/test_architecture_runner.py`) and add one
  `MutationCase` to `MUTATIONS` in
  `tests/integration/test_architecture_owner_rule_mutations.py`, kept
  sorted by `guard_id`, that disables ONLY that guard.
- Never patch the owner under test in the test.

### Step 7 -- run green, then the neighbours

Rerun the same node green. Then run the module and every test that
shares the touched owner (`grep -rln <owner symbol> tests/`). Run the
lint contract before pushing.

### Step 8 -- record it

- Bug fix: add a row to `bugs` in
  `tests/fixtures/lifecycle_bug_ledger.json` (template in
  `assets/templates.md`), sorted by issue. Validate with
  `uv run --extra dev pytest tests/quality/test_lifecycle_bug_ledger.py -q`.
  Every Fixed line in `CHANGELOG.md` maps to a ledger row.
- New promise: when a promise catalog exists, add its id there.

### Step 9 -- execute every printed hint

Every hint the CLI prints ("Run apm ...") is a test case: run it and
assert that it reaches the stated outcome. Assert on every output
line: non-ASCII, duplicates, stale command names, errors on stdout.

## Cross-cutting invariants (after EVERY mutating command)

| Id | Invariant |
|---|---|
| I1 | The lockfile matches the deployed files. |
| I2 | Repeating a converged command changes no bytes. |
| I3 | `install --frozen` and `audit --ci` agree, with the same remediation. |
| I4 | Gates are monotone: a deny, revoked approval or stricter policy never increases what is deployed or runs; the next mutating command removes what is no longer allowed. |
| I5 | Each command writes only its declared write set and never crosses scopes. |
| I6 | Output is ASCII. |
| I7 | Nothing is skipped silently; a failed command preserves state. |
| I8 | The command's documented exit contract holds. |
| I9 | No duplicate or noisy lines. |

Defaults that hide defects (use only when they are the subject):
`--no-policy` in E2E; mock-only assertions; patching the owner under
test; a sample package that is not a twin of the real one;
anti-oracles (assertions that pin current wrong behavior). Credit
comes only from oracle evidence; deleting an oracle call must uncover
its obligation (negative control).

## Moving, merging or retiring lifecycle tests

Read `references/migrate-and-merge.md` before you move a test, fold an
old `*_lifecycle.py` into the family, or delete a test. A test goes
only if it maps to no promise, or a stronger test of the same cell
strictly subsumes it. An exact or equal copy may be removed when its
twin is kept (same body, fixtures, markers, conftest scope, CI lane).
Name files by promise or owner, never by phase, wave or gap.

## Output (PR body evidence row, one per promise)

```text
promise: <id> -- <sentence> (source: <path>)
cell: <factor tuple>   owner: <owner id or path>
instrument: <today|target> <row|rule|scenario|security|journey|test> -> <node id> (extends|new: <why>)
oracles: [I1, I4, I7, snapshot-absence, ...]  decisive: <assertion>
failing-before: <cmd> -> <result line>
passing-after: <cmd> -> <result line>
guard+mutation: <rule id> | n/a     ledger: <issue> | n/a
hints executed: <cmd -> outcome> | none printed
```

## References (load on trigger)

- `references/family.md` -- Step 0 and Step 3: world, fixtures, file
  location.
- `references/oracles.md` -- Step 4: assertions for I1-I9, strength
  floor, anti-oracles, receipts.
- `references/migrate-and-merge.md` -- ONLY when moving, merging or
  retiring tests.
- `assets/templates.md` -- Step 4 and Step 8: pytest skeleton (today),
  replay row (target), bug-ledger row.
