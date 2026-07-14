# PR1: Policy Cache Correctness

## Exact Revisions

- Branch: `danielmeppiel-implement-policy-cache`
- PR: `microsoft/apm#2193`
- Merge base: `5875577df74f646cebaca911673db8bb9d4dd637`
- Code evidence head: `203dacc72142a2cf9ce713d8d7554a80200f3c19`

The branch starts directly from `origin/main` at the merge base above.
Planning artifacts remain in the separate execution ledger and do not
contribute a path or commit to this product PR.

## Failing Contract

The regression tests and mutation probes established these failures before the
corresponding fixes:

- Field-complete cache contract: `4 failed, 1 passed`; omitted leaves included
  `dependencies.require_pinned_constraint`,
  `manifest.require_explicit_includes`,
  `registry_source.allow_non_registry`, and
  `security.integrity.require_hashes`.
- Strict-only policy classification: `8 failed`; every case was incorrectly
  classified empty.
- Completed-chain persistence and cache intent: `5 failed`; weak leaf cache,
  extends-only parent skip, ancestor `no_cache=False`, missing leaf hash, and
  ADO weak-leaf persistence were reproduced.
- Command owner routing: `3 failed`; status and approval still called the
  lower-level discovery path.
- Architecture owner registration: `1 failed`; the cached-policy owner row and
  guard were absent.
- Wave-2 command coverage: `3 failed` with `AttributeError` after the lower-level
  command symbol was removed.
- Backend and stale-chain review folds: GHE/ADO parent routing and stale strict
  parent semantics reproduced six failing regressions before correction.
- Approval warning boundary: nine benign/error/lifecycle cases failed before
  warning classification and logger summary handling were corrected.

## Durable Fact and Canonical Owner

The durable cached-policy shape is owned by
`policy/discovery.py::_policy_to_dict` via `_serialize_policy`.
`_write_cache` is the sole cache writer and all transport and merged-chain
writes route through it.

The approval fallback failure vocabulary is owned by
`policy/outcome_routing.py::POLICY_RESOLUTION_FAILURE_OUTCOMES`.
`commands/approve.py` consumes that vocabulary and does not redeclare its
outcome strings.

Both owners have behavioral regressions and focused static checks in
`scripts/lint-architecture-boundaries.sh` plus matching assertions in
`tests/integration/test_architecture_authorities.py`.

## Owner-Routed Change

- Cache schema version moved from `4` to `5`.
- `_policy_to_dict` explicitly projects every `ApmPolicy` leaf, including
  inheritance, strict dependency and manifest controls, registry source,
  security audit/integrity, binary deployment, and executable policy.
- Optional unmanaged-file values preserve `None` versus explicit empty tuples.
- Extending leaves are not cached by URL, GitHub, or ADO fetchers. Only a
  completed merged chain is persisted under the leaf key.
- The merged entry retains the leaf raw-bytes hash used by project hash-pin
  verification.
- `no_cache` reaches every ancestor.
- GHE and ADO parent references preserve the leaf backend and pass through
  existing authenticated fetchers after same-host validation.
- Stale ancestors remain `cached_stale`, retain refresh diagnostics, and are
  never rewritten as fresh merged entries. Incomplete chains take precedence.
- Policy status, approval, and related callers route through
  `discover_policy_with_chain`.

## Focused Validation

Executed on code evidence head `203dacc72142a2cf9ce713d8d7554a80200f3c19`:

```text
207 passed in 3.54s
```

All policy tests:

```text
1034 passed, 1 xfailed, 63 subtests passed in 2.41s
```

The xfail is the existing git-semver classification case tracked separately
from this PR.

## Broader Validation

Required PR surface:

```text
18884 passed, 2 skipped, 21 xfailed, 19 warnings in 114.01s
Total coverage: 88.74% (required: 80%)
```

Exact lint mirror with GNU grep 3.12 and Python 3.12.9:

```text
All checks passed!
1489 files already formatted
Pylint R0801: 10.00/10
[+] auth-signal lint clean
[+] architecture boundary lint clean
EXACT_LINT_OK MAX_LINES=2100
```

Generated instruction and lock evidence:

```text
cmp source/deployed architecture instructions: exit 0
apm audit --ci: All 9 check(s) passed
```

Docs sync returned `no_change` with high confidence: no command, flag, output,
error text, policy schema key, or Starlight page changed.

## Mutation Break

The following temporary mutations failed their protecting tests and were
restored before commit:

- Writer passed `ApmPolicy()` instead of the supplied policy: owner route test
  failed at `calls == [policy]`.
- Alternate named policy serializer: architecture guard exited `1` with the
  cached-policy owner message.
- Post-merge outcome forced to `found`: permissive-chain test failed
  `found != empty`.
- URL extending leaf written before parent completion: weak-cache regression
  failed.
- Serialized strict deny field removed: cold/warm parity failed.
- Completed empty chain write suppressed: cache-write assertion failed.
- `executables.enforce` removed from `_is_policy_empty`: actionable-leaf case
  failed.
- Approval warning removed or benign `absent` added to failure outcomes:
  warning regressions failed.
- GHE host qualification or ADO backend routing removed: backend parity tests
  failed.
- Stale ancestor relabeled or written as fresh: stale strict-chain tests failed.
- Nearest-stale selection, incomplete-chain precedence, and GHE `extends: org`
  routing mutations each failed their focused regressions.

A literal `_serialize_policy` body mutation is unreachable by the behavioral
owner test because that test monkeypatches `_serialize_policy` before
`_write_cache` executes. The equivalent writer-call mutation above proves the
intended boundary. The static guard separately pins `_serialize_policy(policy)`
to `_policy_to_dict(policy)`.

## Same-Scope Discoveries

This section names the defects independently; `Owner-Routed Change` records the
corresponding remedies.

Each same-promise defect was folded into PR #2193:

1. Omitted cached policy fields weakened warm enforcement.
2. Cached unmanaged-file `null` collapsed to explicit empty.
3. Strict-only policies were classified empty.
4. Extends-only leaves skipped strict parents.
5. GitHub, URL, and ADO transports could persist incomplete weak leaves.
6. Merged caches lost the leaf raw-bytes hash.
7. Ancestor discovery lost `no_cache`.
8. `apm policy status` bypassed chain-aware discovery.
9. Executable approval bypassed chain-aware discovery.
10. Existing integration tests patched a removed command symbol.
11. The strict classifier lacked a future-field drift trap.
12. Approval fallback failures were silent, then initially warned for benign
    no-policy outcomes until the warning boundary was corrected.
13. GHE and ADO parent chains lost backend context.
14. A stale strict parent was overwritten as `found` and could bypass its
    `fetch_failure: block` contract.
15. Nearest-stale and farther-incomplete precedence lacked regression traps.

## Cross-Scope Deferrals

- `microsoft/apm#2201` tracks the pre-existing `bin_deploy` deprecation warning
  produced when canonical cache YAML is re-parsed. This PR keeps the explicit
  field-complete cache shape; deciding whether to omit default legacy fields or
  suppress warnings for canonical cache input is a separate compatibility
  decision.
- OPUS-F3 suggested documenting ancestor integrity and plaintext HTTP parent
  behavior. No issue was filed: `discover_policy` already rejects `http://`
  policy URLs, and the public discovery contract explicitly states that the
  project hash pin applies to the leaf while parent policies are the leaf
  author's responsibility. The proposed defect was not reproducible.

## Review Routing Receipts

Three independent high-effort review lanes evaluated the product evidence tree
for PR #2193:

- GPT-5.6 Luna reviewed failure fidelity, field completeness, cold/warm parity,
  mutation strength, and test tiers.
- Claude Sonnet 5 reviewed canonical ownership, command routing, scope
  boundaries, source/deployed/lock parity, and reviewability.
- Claude Opus 4.8 reviewed policy and governance safety, backend-aware
  inheritance, stale and incomplete chains, hash behavior, and KISS.

The repository APM Review Panel advisory was posted on PR #2193. Its two
in-scope recommendations (permissive merged-chain outcome regression and
CHANGELOG entry) were folded. Later material findings were rerouted through the
three exact model lanes above until no blocking finding remained.

## CI and Mergeability

Local validation is green on the code evidence head. Hosted CI and the live
mergeability probe run on the packaging-corrected branch after this report
update; the final PR handoff records those present-state results.

## Final Disposition

The code evidence head is approved by all three required model lanes and has no
remaining foldable correctness finding. Candidate disposition is
`ready-to-merge` only after the report-bearing head matches the PR, GitHub CI is
observed green, and the live mergeability probe returns mergeable. Do not merge
from this report alone.
