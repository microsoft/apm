---
title: Conformance statement
description: The APM CLI's version-qualified OpenAPM requirement bindings and their limits.
sidebar:
  order: 2
---

The OpenAPM assessment inventory binds requirements to collected tests. It is not a runtime pass certificate or a claim of ratification. [OpenAPM v0.1](../openapm-v01/) remains active with its original contract.

:::note[Planned]
Normative reconciliation is complete for this candidate's [OpenAPM v0.2.0 corrective draft](../openapm-v020/). Qualified-human review, ratification, and activation remain unsatisfied. [Section 9.3](../openapm-v020/#93-amendment-process) requires a labelled process issue and at least two qualified nonauthor human approvals: one implementation reviewer and one consumer/integrator reviewer. No qualifying human approvals are recorded for this candidate.

Only the public-comment period is [waived](https://github.com/microsoft/apm/issues/2818#issuecomment-5558647529). Explicit human ratification/publication remains required; there is no dedicated automatic ratification job, and green `spec-conformance` does not ratify. Assessment work does not advance `latest`, activate or publish the draft, or establish implementation conformance.
:::

## Where the statement lives

Two artifacts live at the repository root and update when the conformance inputs change:

- [`CONFORMANCE.md`](https://github.com/microsoft/apm/blob/main/CONFORMANCE.md) -- per-requirement static bindings (`active`, `skipped`, `xfail`, or `unbound`), requirement links, and waiver rationale.
- [`CONFORMANCE.json`](https://github.com/microsoft/apm/blob/main/CONFORMANCE.json) -- the same inventory with collected test node IDs, exact specification identity, and input fingerprints.

The [spec-conformance workflow](https://github.com/microsoft/apm/blob/main/.github/workflows/spec-conformance.yml) compares regenerated artifacts with the committed copies. Selection is owned by `tests/spec_conformance/_manifest.py`; it validates the manifest identity against the specification artifact. Generation collects the full selected suite afresh and rejects failed collection or mismatched fingerprints instead of reusing an older map.

## How to verify yourself

The conformance suite runs against the selected in-tree specification. At the source revision being assessed, run:

```bash
uv run --extra dev pytest tests/spec_conformance
uv run --extra dev python -m tests.spec_conformance.gen_statement
git diff -- CONFORMANCE.md CONFORMANCE.json  # compare to the in-repo copies
```

The pytest invocation executes the tests and reports test outcomes. The generator separately collects static bindings; it does not consume those execution outcomes. Retain the execution log and exact source or build pin as separate evidence. The final command compares generated artifacts with their committed copies.

## What conformance does NOT cover

An `active` binding does not establish that a test ran or passed. Some bindings inspect schema or specification text rather than running a full lifecycle. Full conformance still needs the evidence and limitations required by Section 11.2 and req-cf-002; source-level results do not substitute for hosted-runtime evidence or certify implementation quality, performance, or operational fitness.

The corrective-draft local-source cases assess req-mf-016 only. They do not prove that the CLI ever satisfied the previous minor's blanket project-root refusal. Preserving that artifact does not claim that the current suite proves historical compliance. The requirements manifest remains informative, and existing wire-schema identities are unchanged.

The audit cases bind current target intent and read-only replay to req-lk-023. They do not erase inherited integrity requirements: the CLI's bare content audit uses source-derived drift, while its stored-hash and full-SHA consistency baselines run in CI/conformance audit. The unqualified audit obligation in req-lk-017 remains an explicit bare-mode conformance limitation. Native Cowork cases use pre-existing fixture state, not a successful native install round trip.

One coupled source-level case is a strict expected failure: local installation
correctly dereferences an internal resource link, but unchanged CI audit can
report that deployed regular file as orphaned. The case still checks content
integrity and unchanged project/HOME state; escaping links have a separate,
unsuppressed refusal control. This limitation is not a conformance pass or a
symlink-containment exception.

The retained manifest schema is also incomplete as an acceptance oracle: it
rejects some source-plus-modifier forms and structurally accepts invalid
`policy.hash` strings. Those schema probes do not prove runtime digest
enforcement. Git symlink, submodule and checkout-filter hashing boundaries lack
cross-platform execution evidence here. The normative obligations remain intact;
the generated inventory lists these limitations rather than waiving them.

For the amendment workflow, see [Adding or changing a normative requirement](https://github.com/microsoft/apm/blob/main/CONTRIBUTING.md#adding-or-changing-a-normative-requirement-openapm).
