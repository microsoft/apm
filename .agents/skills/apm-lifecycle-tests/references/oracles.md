# Oracles: what to assert and how

Load at Step 4 when writing assertions. Every assertion observes
durable state or user-visible output, never implementation calls.

## Snapshot pair

```python
before = LifecycleStateSnapshot.capture(project, targets=(target,))
result = runner.run(args, cwd=project, env=env)
after = LifecycleStateSnapshot.capture(project, targets=(target,))
```

User scope: pass `external_roots=` (see `_apm_home_root` in
`tests/integration/test_required_lifecycle_state_machine.py`). Write
sets across roots: `ArtifactSnapshotSet.capture({"project": ..., "home": ...})`
before and after (`tests/utils/artifact_snapshot.py`).

## Cross-cutting invariants (after every mutating step)

| Id | Assert | Typical code |
|---|---|---|
| I1 lock matches deployed | every recorded deployment exists on disk with a matching hash; no APM-owned file lacks a record | for each `record` in `after.deployment_records`, `(root / record.locator.value).exists()`; compare lockfile `deployed_file_hashes` to sha256 of the files. `audit --ci` is a second opinion, not a substitute |
| I2 idempotence | rerunning the converged command leaves lockfile and deployed bytes unchanged | `_assert_same_state(after, after_rerun)` or snapshot equality; compare `lockfile_bytes` and `files`, not only `semantic_bytes` |
| I3 frozen equals audit | `install --frozen` exits 0 iff `audit --ci` exits 0 on the same state, and both name the same remediation | run both; compare exit codes and the remediation line (`_install_audit_replay_kit.py` worlds) |
| I4 gate monotonicity | after a deny, revoke or stricter policy the deployed set is a subset of the previous one; after the next mutating command the disallowed items are gone, announced | snapshot-absence on hook, bin, MCP config entry and LSP config (`assert_paths_absent`) |
| I5 write set and scope | only the declared paths changed; project commands never touch user scope and vice versa | `assert_snapshot_changes_within(...)` / `assert_only_snapshot_paths_changed(...)` per root id |
| I6 ASCII | stdout and stderr are ASCII | `result.stdout.isascii() and result.stderr.isascii()` |
| I7 no silent success | every skip, park or fallback is announced with a reason; a failed command leaves state unchanged | assert the announcement line; `_assert_same_state(before, after)` on failure |
| I8 exit contract | the documented codes hold: 0 success, 1 error, 2 usage; `apm pack` also documents 3, 4, 5 in `apm pack --help` | `assert result.returncode == expected_exit`, never just `!= 0` |
| I9 no noise | each status line (`[+]`, `[!]`, `[x]`, `[i]`, `[*]`, `[>]`) appears once per cause; no tracebacks; no warnings for absent features | count status lines per stream (table borders may repeat) |

Target strength. When `tests/lifecycle/` is present, its oracles are
currently weaker than this table: I3 compares exit codes only, not
remediation yet; I7 announcements are checked only through a row's
`announce` field; I9 checks consecutive duplicate lines, duplicate
integrated lines and tracebacks, with no per-cause count; I8 uses the
family's exit contracts (audit 0-3, pack 0-5, default 0-2). Add the
missing strength in your test when the promise depends on it.

Route every `CommandResult` through one helper that applies I6 and I9
(see `_assert_output_oracles` in `assets/templates.md`), so a new
command cannot skip them.

## Strength ladder

From strongest to weakest: e2e-snapshot (installed CLI + before/after
snapshot); e2e (installed CLI + user-visible assertion); cli
(CliRunner or engine); component (real fs or git, at most one mock);
unit.

Floors:

- lifecycle promises and I1-I9: e2e-snapshot;
- owner decisions: table-driven test at the owner symbol (owner not
  patched) plus one E2E in the family;
- spec requirements: exercise the implementation, not the spec text;
- gate monotonicity and egress: e2e plus snapshot-absence or an
  egress log (`requests_to`, `hosts_receiving` on the scripted proxy).

## Decisive assertion

The assertion that fails before the fix is a state fact: file present
or absent, bytes equal, snapshot equivalence or bounded diff, lockfile
row present or absent (parse YAML, do not grep text), egress log
contains or lacks a host, exact exit code. Output text proves I6, I8,
I9 and announcements; it is never the only proof of a durable promise.

## Anti-oracles (do not write; flag when found)

- A string or ledger-field compare as the only assertion in a
  lifecycle file: a unit test relocated into a lifecycle file, worse
  than nothing because the filename implies a guarantee.
- An assertion that pins current wrong behavior (for example a
  non-ASCII table or a duplicated line expected in output). It must
  change together with the fix.
- `mock.assert_called*()` as the only proof of a user-visible message.
- A refusal (exit 2 on an unsupported flag combination) used as
  evidence for the success case.
- A test that patches the owner it claims to verify.

## Negative control

Before claiming coverage, delete or invert the decisive assertion (or
the oracle call) and rerun: the test must stop detecting the defect.
Restore it and record the command and result.

## Receipts (TARGET family, when present)

When `tests/lifecycle/receipts.py` is present, credit is issued only
through `receipts.emit` from the oracles (any other caller raises
`ReceiptForgeryError`), never by the test body or a marker. A receipt records
node and parameters, pre-state digest, argv and scope, post-state
evidence (snapshot digest, egress log or transcript), oracle id,
outcome and CI lane. Skips, early returns, labels, markers and
Hypothesis events earn nothing. Do not write receipts by hand.
