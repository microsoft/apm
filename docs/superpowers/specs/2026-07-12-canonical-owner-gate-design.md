# Canonical Owner Gate Design

## Problem

The bug shepherding run produced correct fixes, but its terminal gate did not
prove that every durable decision still had one canonical owner. PR #2177
therefore reached ready-to-merge with behavioral regression coverage but
without the static boundary guard required by
`.github/instructions/architecture.instructions.md`.

The immediate findings span four PRs:

- #2177 preserves a locked skill subset during audit replay but lacks the
  static half of the dual guardrail.
- #2176 centralizes skill-subset matching, but its lexical guard can miss a
  renamed reimplementation.
- #2094 introduces a stable Windows executable path without a cross-file
  production boundary guard.
- #2170 fixes hundreds of relative documentation links but leaves three
  instances unfixed because the current docs validator does not validate
  relative destinations.

The durable fix must repair those PRs and make future bug shepherding fail
closed when canonical-owner evidence is incomplete.

## Goals

1. Remediate all four existing PRs without mixing their scopes.
2. Require every shepherded bug fix to classify its architecture impact.
3. Require both behavioral and static evidence for a new owner,
   centralization, or split-authority repair.
4. Keep ordinary behavioral fixes lightweight while recording why no
   architecture guard is required.
5. Preserve exact-head, mutation-break, CI, and authorship evidence.

## Non-goals

- Replacing the existing architecture boundary script.
- Building a general-purpose semantic analyzer for arbitrary Python.
- Refactoring unrelated owner boundaries.
- Folding the shepherd workflow changes into any of the four bug PRs.

## Design

### 1. PR #2177: lockfile-to-replay intent preservation

`LockedDependency.to_dependency_ref()` remains the owner of reconstructing the
persisted subset. `run_replay()` remains a consumer that forwards that value
to integration without interpreting it.

Add an AC4 boundary check that fails when either seam is removed:

- `to_dependency_ref()` must populate `skill_subset` from
  `self.skill_subset`.
- the replay call to `integrate_package_primitives()` must populate
  `skill_subset` from `package_info.dependency_ref.skill_subset`, not a
  constant or independently derived value.

Add the matching assertion to
`tests/integration/test_architecture_intent_guards.py`. The committed
behavioral tests remain the symptom-level guard. Mutation probes will remove
each propagation edge independently and prove that the behavioral and
architecture gates fail.

### 2. PR #2176: semantic subset-owner hardening

`models/dependency/subsets.py::skill_subset_filter_tokens()` remains the sole
owner of translating source-relative subset entries into raw, normalized, and
leaf-name match tokens.

Strengthen the existing boundary beyond retired function names. The guard
will inspect the two production consumers and reject local token-normalization
shapes, including:

- importing or calling `PurePosixPath` for subset matching;
- slash normalization or leaf extraction inside a function that consumes a
  skill subset;
- constructing a local token set instead of calling the owner.

The matching architecture test will parse the consumer modules with `ast` and
assert that the owner call is the only subset-token transformation. A renamed
helper carrying the former algorithm will be used as the mutation probe and
must fail.

The check is deliberately scoped to the integrator and exporter consumers.
It does not claim to infer arbitrary data flow across the whole repository.

### 3. PR #2094: Windows stable-executable owner

Declare `install.ps1` as the owner of the version-stable executable location:

```text
$installRoot/current/apm.exe
```

Extend the architecture boundary script with two checks:

- the owner must define `$currentDir` from `$installRoot`, define
  `$currentExe` from `$currentDir`, and add `$currentDir` to PATH;
- production PowerShell, workflow, and Python files outside `install.ps1`
  must not independently derive the stable path.

Windows E2E and unit tests are validators and remain allowed to reconstruct
the expected path. The matching architecture test will assert the owner
contract, the production-file allowlist, and the guard label. A mutation probe
will add a second production derivation and prove the lint fails.

### 4. PR #2170: relative documentation link integrity

Correct the three remaining `ssl-issues` links.

Add a post-build checker under `docs/scripts/` that walks generated HTML,
resolves relative `href` values from each page URL, and verifies that each
destination maps to a generated file or directory index. It will ignore
external URLs, anchors, mail links, query-only links, and generated asset
references. Because it runs against generated HTML, fenced Markdown examples
do not become false positives.

Wire the checker into `npm run build`. Reverting any of the three corrected
links must make the docs build fail.

### 5. Shepherd-driver architecture gate

Add a mandatory architecture classification after follow-up folding and
before the push gate. Every PR receives exactly one classification:

- `ordinary-fix`
- `owner-extension`
- `new-owner`
- `split-authority-repair`
- `not-applicable`

The driver records each durable decision touched, its canonical owner, and
the consumer routing evidence. `new-owner` and `split-authority-repair`
require the dual guardrail. `owner-extension` requires it when the change
centralizes or repairs routing; otherwise the driver records why existing
guards cover the new case.

The completion schema gains an `architecture_evidence` object. A
`ready-to-merge` return requires it. When `dual_guardrail_required` is true,
the schema and prompt require:

- behavioral regression test;
- static boundary guard;
- matching `tests/integration/test_architecture_*.py` assertion;
- mutation-break evidence;
- successful architecture boundary command.

Missing evidence returns `blocked` or remains in the convergence loop. It
cannot be deferred as out of scope when the PR itself changes the authority.

### 6. Parent orchestrator visibility

`batch-bug-shepherd` will name the canonical-owner gate as a binding invariant,
pass it through greenfield fix prompts, and include the classification and
evidence in its final report. The per-PR enforcement remains owned by
`shepherd-driver`, avoiding duplicate orchestration logic.

Content evals will require:

- architecture classification before ready-to-merge;
- dual-guardrail evidence for authority-affecting fixes;
- a fail-closed terminal result when evidence is absent.

## Data Flow

```text
issue reproduction
    -> implementation and behavioral regression
    -> review follow-ups
    -> architecture classification
    -> canonical owner trace
    -> dual guardrail check when required
    -> architecture lint and mutation evidence
    -> push and observed-green CI
    -> ready-to-merge completion return
```

## Failure Handling

- Unknown architecture impact is not treated as `ordinary-fix`; the driver
  remains in the convergence loop or returns `blocked`.
- A missing owner is resolved by defining one before adding consumers.
- A missing static or behavioral half blocks an authority-affecting fix.
- Intentional boundary exceptions require
  `architecture-authority-exempt: <owner and reason>`.
- Docs-link failures print source page, original `href`, and resolved missing
  destination.
- Contributor-fork push failures continue to use the existing superseding-PR
  path with authorship trailers.

## Validation

Each existing PR is tested at its exact head in an isolated worktree.

- #2177: focused drift tests, architecture intent tests, boundary lint, and
  two propagation mutation probes.
- #2176: focused subset tests, architecture authority tests, boundary lint,
  and a renamed-normalizer mutation probe.
- #2094: Windows launcher unit tests, architecture authority tests, boundary
  lint, and a second-owner mutation probe. Windows-only E2E remains CI-gated.
- #2170: docs build and a reverted-link mutation probe.
- Workflow hardening: primitive content evals, completion-schema validation,
  canonical lint, and review of the generated skill surfaces.

## Delivery

The four remediations are committed and pushed directly to their existing PR
branches. The workflow change is delivered as a fifth focused PR from
`harden/architecture-owner-gate`, containing this design and the primitive
changes. No existing PR absorbs unrelated shepherd orchestration scope.
