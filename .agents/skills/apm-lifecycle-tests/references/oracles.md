# Oracles: how to check I1-I9 and where evidence comes from

Load at Step 4 when writing assertions. An oracle is code that decides
pass or fail from observable state. Every check below names the
helper to use today; the target family moves the same checks into
`tests/lifecycle/oracles.py::check_cross_cutting`.

## Snapshot pair (the base of every oracle)

```python
before = LifecycleStateSnapshot.capture(project, targets=(target,))
result = runner.run(args, cwd=project, env=env)
after = LifecycleStateSnapshot.capture(project, targets=(target,))
```

For user scope, pass `external_roots=(LifecycleStateRoot(root_id="apm-home",
...),)` (see `_apm_home_root` in
`tests/integration/test_required_lifecycle_state_machine.py`). For an
open-world write-set check, also capture
`ArtifactSnapshotSet.capture({"project": project, "home": isolated.home})`
before and after (`tests/utils/artifact_snapshot.py`).

## Invariant checks

| Id | Check (today) |
|---|---|
| I1 lockfile matches deployed | For every `record` in `after.deployment_records`, `(root / record.locator.value).exists()`; and no APM-owned file is on disk without a record. `audit --ci` (`deployed-files-present`, `drift` checks) is a second opinion, not a substitute. |
| I2 idempotent | Run the converged command again; `_assert_same_state(after, again)` (canonical module) or `LifecycleStateSnapshot` equality. Compare `lockfile_bytes` and `files`, not only `semantic_bytes`. |
| I3 frozen == audit | Run `install --frozen` and `audit --ci --format json` on the same state; assert both pass, or both fail naming the same remediation. Use `_install_audit_replay_kit.py` worlds. |
| I4 gate monotone | After a deny, revoked approval or stricter policy, the set of deployed executables, hooks, MCP and LSP entries is a subset of the set before; after the next mutating command, entries no longer allowed are gone (`assert_paths_absent`), and the removal is announced. |
| I5 write set | `assert_only_snapshot_paths_changed(before_set, after_set, allowed_paths={...})` or `assert_snapshot_changes_within(...)`. Project commands must leave the `home` root unchanged and vice versa. |
| I6 ASCII | `result.stdout.isascii() and result.stderr.isascii()` on every command. |
| I7 no silent success | A skipped item has a status line saying so. A failed command (non-zero exit) leaves `after == before` (`_assert_same_state`). |
| I8 exit contract | Assert the exact `returncode` the command documents (`apm <cmd> --help`, docs reference page). Never `!= 0` when the contract names a code. |
| I9 no noise | No duplicate status lines (`[+]`, `[!]`, `[x]`, `[i]`, `[*]`, `[>]`) per stream; errors on stderr or in a status line, never a traceback. Table borders may repeat. |

Apply I6 and I9 to every `CommandResult` through one helper (the
template's `_assert_output_oracles`), so a new command cannot skip
them.

## Decisive assertion

The assertion that fails before the fix must be a state fact:

- file present or absent, bytes equal (`read_bytes()`),
- snapshot equivalence or a bounded diff,
- lockfile row present or absent (parse YAML; do not grep text),
- egress log contains or lacks a host (loopback proxies in
  `tests/utils/local_header_capture_proxy.py`,
  `tests/utils/local_scripted_https_proxy.py`),
- exact exit code.

Output text is evidence of I6, I8, I9 and announcements. It is never
the only proof of a durable promise. A lifecycle test whose only
effective assertion is a string or ledger-field comparison is a unit
test relocated into a lifecycle file; the filename implies a guarantee
that is absent. Replace it with a state oracle or flag it.

## Anti-oracles

An anti-oracle asserts current wrong behavior (it would have to change
together with the fix). Signs: the assertion names a defect symptom as
expected output; the docstring says "currently"; a TODO points at an
issue. Do not add one. When you find one, rewrite it to assert the
promise and mark it `xfail(strict=True, reason=<issue>)` only if the
fix is out of scope, or delete the assertion and record why.

## Negative control

Before claiming coverage, prove the oracle is load-bearing: temporarily
delete or invert the decisive assertion (or the oracle call) and rerun;
the test must still exercise the cell but must no longer detect the
defect. Then restore it. Record the command and result in the report.

## Receipts (TARGET family only)

When `tests/lifecycle/receipts.py` exists, credit comes only from
receipts written by the oracle, never by the test body. A receipt
holds: node id, params, pre-state digest, argv, scope, post-state
digest (or egress log or transcript), oracle id, outcome, CI lane.
Labels, markers, skips, Hypothesis events and steps that were
requested but not performed earn nothing. Do not write receipts by
hand and do not add a receipt call outside `oracles.py`.

## The ApmLifecycle engine: why it is the highest-proof instrument

It drives real state transitions (install -> compile -> audit ->
uninstall, target widen then narrow, lock then prune) and snapshots
durable state on both sides. Unit and component tests cannot certify
"this file was not written", "this file was not deleted" or "this
user content survived"; this harness can. Trigger surfaces that need
it:

- install / uninstall / update / compile / audit command paths;
- the deployment ledger, lockfile write path or `deployed_files`;
- target resolution, widening or narrowing, primitive-to-target
  layout mapping;
- any write into a target root (`.github/`, `.claude/`, `.cursor/`,
  `.agents/`, ...) or into user scope (`~/.apm/`);
- `--dry-run` on any command;
- cleanup, pruning, reconciliation, drift or orphan handling.
