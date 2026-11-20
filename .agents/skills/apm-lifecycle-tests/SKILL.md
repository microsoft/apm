---
name: apm-lifecycle-tests
description: >-
  Use this skill when you write, extend or migrate an APM lifecycle
  test: any test that drives the real apm CLI through command
  sequences and checks durable state (files, lockfile, MCP configs,
  approvals, egress). Apply it for a bug fix or behavior change in
  install, update, uninstall, prune, compile, audit, approve or deny,
  pack or bundle, marketplace, policy, MCP or LSP; for a regression
  trap; for a lifecycle_smoke test, state-machine rule, replay row,
  scenario, security table or docs journey; and when a reviewer says
  lifecycle coverage is missing, even if the request just says add an
  e2e or integration test. It names the promise, picks the instrument,
  writes the failing-before test with snapshot and invariant oracles,
  and records the ledger row. Not for pure unit tests, PR review or
  deciding which tests to delete.
---

# apm-lifecycle-tests -- author lifecycle tests from promises

A lifecycle test drives the real `apm` CLI through a command sequence
in a hermetic world and asserts durable state on both sides of every
mutating step. It is the only tier that can prove a NEGATIVE ("this
file was not written", "this user file survived", "the gate removed
what it no longer allows"). In agentic coding, where no human walks
the CLI by hand, this harness IS the human walkthrough.

Boundary: pure unit tests (one function, no CLI) follow the normal
`tests/unit` conventions. Reviewing a PR's test coverage belongs to
the test-coverage-expert persona. Deciding which tests to delete is
out of scope; moving or merging follows
[references/migrate-and-merge.md](references/migrate-and-merge.md).

## Operating rules

- Single thread. Plan, execute, verify by running.
- Every fact is a tool result. "The test passes", "no test exists",
  "this path exists" come from `pytest`, `grep` or `ls` output in
  this session, never from memory or prose.
- Restate the promise id at the top of every step's notes. If you
  cannot name it, you are writing an unexplained test.
- ASCII only in test code, ids, comments and output assertions.

## Procedure

### Step 0 -- locate the family (branch)

Run `ls tests/lifecycle/conftest.py`.

- Present: the target family exists. Use its world, driver, model,
  oracles and receipts. Read the TARGET section of
  [references/family.md](references/family.md).
- Absent: use today's harness (`tests/utils/apm_lifecycle_runner.py`,
  `tests/utils/isolated_apm_environment.py`,
  `tests/utils/lifecycle_state.py`, the `tests/integration/_*_kit.py`
  modules). Read the TODAY section of
  [references/family.md](references/family.md). This branch is
  authoritative until the target family exists.

### Step 1 -- name the promise

A promise is a testable statement APM makes to a user, cited to
source. Pick the highest-precedence source that applies:

1. Cross-cutting invariants I1-I9 (below).
2. Bug-ledger laws: `property_catalog` ids in
   `tests/fixtures/lifecycle_bug_ledger.json`.
3. OpenAPM requirement ids (`req-*`) in `CONFORMANCE.json`.
4. Architecture owner decisions in `.apm/architecture/owners/*.json`.
5. `CHANGELOG.md` guarantees.
6. Docs claims with must / never / fails / exits wording under
   `docs/src/content/docs/`.

Write: `PROMISE: <id> -- <one sentence> (source: <path>)`. A test that
maps to no promise is unexplained, not automatically deletable.

### Step 2 -- derive the cells

State = manifest x lockfile x deployed x cache x policy x approvals x
scope x targets x source kind. Partition a factor only where the code
branches; cite the branch (`file:line` from `grep -n`).

- Group operations by shared code entry: install, update,
  `deps update` and `lock` are one pipeline.
- Each SIDE DOOR (a mutating path that skips the pipeline: local
  bundle, `--mcp`, the `--dry-run` planner, unpack) is its own
  family. Defects cluster there; give each one its own cell.
- Pairwise (t=2) by default. Go to t=3 only on named split-authority
  triples: OP x GATE x PRIM, FROZEN x SCOPE x GATE, OP x SRC x PRIM,
  OP x SCOPE x LOCK, DRYRUN x OP x SRC.
- Scenario suites live outside the factors: history (older lock
  generators), ownership (unmanaged sibling assets), platform,
  failure phase, cache presence, graph/version, concurrency.
- Equivalence is partial; name the exceptions. Hosts behave the same
  except lock identity; targets the same except MCP ownership; file
  primitives the same except compile staging.
- A refusal test never discharges a success obligation.

### Step 3 -- pick the instrument

| Need | Today (no `tests/lifecycle/`) | Target family |
|---|---|---|
| One promise, one command sequence | `ApmLifecycleRunner` test in `tests/integration/test_<promise>_lifecycle.py` | replay row YAML |
| Transition reachable from many states | extend `tests/integration/test_required_lifecycle_state_machine.py` or `test_generated_lifecycle_state_machine.py` | rule in `machines/test_m_*` |
| History, ownership, platform, failure phase, cache, graph, concurrency | scenario test on a `_*_kit.py` world | `scenarios/<suite>` |
| Gate, policy, integrity, egress, credentials | table-driven E2E with egress log | `security/` table row |
| Printed hint or documented journey | real-CLI journey, hint executed | `journeys/<persona>/*.yaml` + golden |
| Owner decision | table-driven test at the owner symbol + one E2E | same |

Prefer extending an existing test (a new assertion or parametrize
case) over a new module; lifecycle tests are expensive in CI shard
time. Say which existing test you extend, or why a new module is
needed. When the surface is per-target, derive parameters from
`KNOWN_TARGETS` (`src/apm_cli/integration/targets.py`) instead of
hardcoding target names.

### Step 4 -- write the failing-before test

Start from
[assets/templates/test_lifecycle_promise.py.txt](assets/templates/test_lifecycle_promise.py.txt)
(today) or [assets/templates/replay-row.yaml](assets/templates/replay-row.yaml)
(target family).

- Fresh hermetic world: isolated HOME and APM_HOME, local packages or
  local bare git repos via insteadOf, no network.
- Capture stdout, stderr and exit code separately for every command.
- Snapshot durable state BEFORE and AFTER each mutating command
  (`LifecycleStateSnapshot.capture`), then apply the I1-I9 oracles.
  Read [references/oracles.md](references/oracles.md) when writing
  the assertions.
- The decisive assertion is an observable state fact: a file present
  or absent, bytes unchanged, a snapshot equivalence, an egress log
  without a request. A string compare alone is not lifecycle proof.

### Step 5 -- run red

Run the test targeted against the unfixed code and keep the output:

```bash
uv run pytest <node-id> -n 0 -x -q
```

Packaged-binary tests need an apm binary. For local runs,
`APM_BINARY_PATH="$(uv run which apm)"` drives the in-tree CLI. A test
that is green before the fix does not trap the defect; tighten the
oracle until it fails for the stated reason. Paste the failing
assertion line into your notes under the promise id.

### Step 6 -- fix at the owner

Name the owner (the `owner` field in `.apm/architecture/owners/*.json`).
Route every caller through it. When the defect came from a caller
bypassing the owner, add a closed-world guard that refuses new callers
plus a mutation case that disables ONLY that guard: register the rule
id in `_EXPECTED_RULE_ID_TEXT`
(`tests/unit/scripts/test_architecture_runner.py`) and add a
`MutationCase` to `MUTATIONS` in
`tests/integration/test_architecture_owner_rule_mutations.py`, kept
sorted by `guard_id`. Never patch the owner under test in the test.

### Step 7 -- run green, then the neighbours

Rerun the same node green. Then run the module and every test that
shares the touched owner (`grep -rln <owner symbol> tests/`).

### Step 8 -- record the ledger row

For a fix, add a row to `bugs` in
`tests/fixtures/lifecycle_bug_ledger.json` from
[assets/templates/ledger-row.json](assets/templates/ledger-row.json):
`issue`, `fixed_in`, `summary`, `failure_modes`, `properties`,
`regression_tests` (node ids). Keep `bugs` sorted by issue. Validate:

```bash
uv run pytest tests/quality/test_lifecycle_bug_ledger.py -q
```

Every Fixed line in `CHANGELOG.md` maps to a ledger row.

### Step 9 -- execute every printed hint

Every CLI hint is a test case: run the command the CLI tells the user
to run and check it reaches the stated outcome. Assert on every output
line: non-ASCII, duplicate lines, stale command names, errors printed
to stdout.

## Cross-cutting invariants (check after EVERY mutating command)

| Id | Invariant |
|---|---|
| I1 | The lockfile matches the deployed files. |
| I2 | Repeating a converged command changes no bytes. |
| I3 | `install --frozen` and `audit --ci` give the same verdict and remediation. |
| I4 | Gates are monotone: a deny, revoked approval or stricter policy never increases what is deployed or runs, and the next mutating command removes what is no longer allowed. |
| I5 | Each command writes only its declared write set and never crosses scopes. |
| I6 | Output is ASCII. |
| I7 | Nothing succeeds silently: skips are announced; a failed command preserves state. |
| I8 | Exit codes follow each command's documented contract. |
| I9 | No duplicate or noisy lines. |

## Required strength

| Promise kind | Minimum evidence |
|---|---|
| Lifecycle behavior, I1-I9 | installed CLI (packaged binary; `python -m apm_cli.cli` only for exploration) + before/after snapshot |
| Owner decision | table-driven test at the owner symbol (owner not patched) + one E2E in the family |
| Spec requirement | exercise the implementation, not the spec text |
| Gate monotonicity, egress | E2E + snapshot or egress log showing absence |

Defaults that hide defects -- do not use them unless they are the
subject under test:

- `--no-policy` in E2E (allowed only when testing `--no-policy`).
- Mock-only assertions; patching the owner under test.
- A hermetic sample package that is not a twin of the real one.
- Anti-oracles: assertions that pin current wrong behavior and would
  have to change together with the fix. Replace or flag them.
- Credit from labels, markers, skips, Hypothesis events or
  requested-but-unperformed steps. Only oracle evidence counts; the
  negative control is that deleting an oracle call must uncover its
  obligations.

## Naming

Name files by promise or owner (`test_<promise>_lifecycle.py`), never
by phase, wave or gap. Module docstring: first line states the
promise; body cites the source and issue.

## Report template

Return this block when done (one per promise):

```text
PROMISE: <id> -- <sentence> (source: <path>)
CELLS: <factor=value, ...>; side door: <name|none>; t=<2|3> (<triple>)
INSTRUMENT: <today|target> <kind> -> <file>::<test> (extends|new: <why>)
ORACLES: snapshot before/after; I1..I9 checked: <list>; decisive: <assertion>
RED: <command> -> <failing assertion line>
GREEN: <command> -> <N passed in Xs>
OWNER: <owner path>; guard+mutation: <rule id | n/a>
LEDGER: <issue> -> <properties> -> <node ids> | n/a (not a fix)
HINTS EXECUTED: <command -> outcome | none printed>
```

## References (load on trigger)

- [references/family.md](references/family.md) -- load at Step 0 and
  Step 3 to choose the world, helper and file location.
- [references/oracles.md](references/oracles.md) -- load at Step 4
  when writing assertions for I1-I9 or receipts.
- [references/migrate-and-merge.md](references/migrate-and-merge.md)
  -- load ONLY when moving, merging or retiring an existing
  `*_lifecycle*.py` file.
- `assets/templates/` -- pytest skeleton (today harness), replay row
  (target family), bug-ledger row.
