---
title: Conformance statement
description: The APM CLI's version-qualified OpenAPM requirement bindings and their limits.
sidebar:
  order: 2
---

The active assessment selects the [OpenAPM v0.2.0 corrective draft](../openapm-v020/). Its generated inventory binds requirements to collected tests; it is not a runtime pass certificate or a claim of ratification. The [previous minor](../openapm-v01/) remains available with its original contract.

## Where the statement lives

Two artifacts ship at the repository root and update when the conformance inputs change:

- [`CONFORMANCE.md`](https://github.com/microsoft/apm/blob/main/CONFORMANCE.md) -- per-requirement static bindings (`active`, `skipped`, `xfail`, or `unbound`), requirement links, and waiver rationale.
- [`CONFORMANCE.json`](https://github.com/microsoft/apm/blob/main/CONFORMANCE.json) -- the same inventory with collected test node IDs, exact specification identity, and input fingerprints.

The [spec-conformance workflow](https://github.com/microsoft/apm/blob/main/.github/workflows/spec-conformance.yml) compares regenerated artifacts with the committed copies. Selection is owned by `tests/spec_conformance/_manifest.py`; it validates the manifest identity against the specification artifact. Generation always collects the full active suite afresh and rejects failed collection or mismatched fingerprints instead of reusing an older map.

## How to verify yourself

The conformance suite is shipped in-tree and runs against the published spec text. To reproduce the statement locally:

```bash
git clone https://github.com/microsoft/apm.git
cd apm
uv run --extra dev pytest tests/spec_conformance
uv run --extra dev python -m tests.spec_conformance.gen_statement
git diff -- CONFORMANCE.md CONFORMANCE.json  # compare to the in-repo copies
```

The pytest invocation executes the tests and reports test outcomes. The generator separately collects static bindings; it does not consume those execution outcomes. Retain the execution log and exact source or build pin as separate evidence. The final command compares generated artifacts with their committed copies.

## What conformance does NOT cover

An `active` binding does not establish that a test ran or passed. Some bindings inspect schema or specification text rather than running a full lifecycle. Full conformance still needs the evidence and limitations required by Section 11.2 and req-cf-002; source-level results do not substitute for hosted-runtime evidence.

Current local-source cases assess the corrective req-mf-016 only. They do not prove that the CLI ever satisfied the previous minor's blanket project-root refusal. Preserving that artifact does not claim that the current suite proves historical compliance. The requirements manifest remains informative, existing wire-schema identities are unchanged, and the `latest` citation is not advanced during draft preparation.

The audit cases bind current target intent and read-only replay to req-lk-023. They do not erase inherited integrity requirements: the CLI's bare content audit uses source-derived drift, while its stored-hash and full-SHA consistency baselines run in CI/conformance audit. The unqualified audit obligation in req-lk-017 remains an explicit bare-mode conformance limitation. Native Cowork cases use pre-existing fixture state, not a successful native install round trip.

For the broader drift-detection story (how the spec authors prevent the spec from rotting relative to the only implementation), see [`CONTRIBUTING.md` -- Spec amendment workflow](https://github.com/microsoft/apm/blob/main/CONTRIBUTING.md#spec-amendment-workflow).
