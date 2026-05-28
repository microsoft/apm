# OpenAPM Conformance Statement -- v0.1.1

Generator: gen_statement.py v1.
Spec: [docs/src/content/docs/specs/openapm-v0.1.md](docs/src/content/docs/specs/openapm-v0.1.md)

This file is generated. Do NOT edit by hand. Run
`uv run python -m tests.spec_conformance.gen_statement` to regenerate.

## Honesty contract

There is NO automated CI detector for spec-vs-behaviour drift beyond the four sets enforced by `orphan_check.py`: spec anchors, manifest entries, Appendix C rows, and `@pytest.mark.req` markers. A requirement marked `status=active` is exercised by at least one assertion. A requirement marked `status=skipped` carries a written waiver below; this is debt, not coverage. A requirement with `status=xfail` is asserted-but-known-broken.

## Conformance classes

Producer, Consumer, Governance classes are exercised below. Registry is waived for v0.1 (the class ships only the trust-anchor MUST at sec.11.3.3; no wire surface exists yet; v0.2 expands).

## Coverage summary

| Class | Active | Skipped | Xfail | Unbound |
|-------|-------:|--------:|------:|--------:|
| Producer | 5 | 7 | 0 | 0 |
| Consumer | 12 | 50 | 0 | 0 |
| Registry | 0 | 1 | 0 | 0 |
| Governance | 4 | 8 | 0 | 0 |

## Per-requirement coverage

| Req ID | Keyword | Sec | Class | Status | Tests |
|--------|---------|----:|-------|--------|------:|
| [req-cf-001](docs/src/content/docs/specs/openapm-v0.1.md#req-cf-001) | MUST | 12.5 | consumer | active | 6 |
| [req-cf-002](docs/src/content/docs/specs/openapm-v0.1.md#req-cf-002) | MUST | 12.3 | consumer | skipped | 1 |
| [req-ext-001](docs/src/content/docs/specs/openapm-v0.1.md#req-ext-001) | MUST | 4.1 | consumer | active | 1 |
| [req-ext-002](docs/src/content/docs/specs/openapm-v0.1.md#req-ext-002) | MUST | 4.1 | producer | active | 1 |
| [req-lk-001](docs/src/content/docs/specs/openapm-v0.1.md#req-lk-001) | MUST | 5.1 | consumer | active | 1 |
| [req-lk-002](docs/src/content/docs/specs/openapm-v0.1.md#req-lk-002) | MUST | 5.4 | consumer | active | 1 |
| [req-lk-003](docs/src/content/docs/specs/openapm-v0.1.md#req-lk-003) | MUST | 5.2 | consumer | active | 1 |
| [req-lk-004](docs/src/content/docs/specs/openapm-v0.1.md#req-lk-004) | MUST | 5.4 | consumer | active | 1 |
| [req-lk-005](docs/src/content/docs/specs/openapm-v0.1.md#req-lk-005) | MUST | 5.5 | consumer | skipped | 1 |
| [req-lk-006](docs/src/content/docs/specs/openapm-v0.1.md#req-lk-006) | MUST | 5.5 | consumer | active | 1 |
| [req-lk-007](docs/src/content/docs/specs/openapm-v0.1.md#req-lk-007) | SHOULD | 5.5 | consumer | skipped | 1 |
| [req-lk-008](docs/src/content/docs/specs/openapm-v0.1.md#req-lk-008) | MUST | 5.6 | consumer | active | 1 |
| [req-lk-009](docs/src/content/docs/specs/openapm-v0.1.md#req-lk-009) | MUST | 5.6 | consumer | skipped | 1 |
| [req-lk-010](docs/src/content/docs/specs/openapm-v0.1.md#req-lk-010) | MUST | 5.6 | consumer | skipped | 1 |
| [req-lk-011](docs/src/content/docs/specs/openapm-v0.1.md#req-lk-011) | MUST | 5.2 | consumer | active | 1 |
| [req-lk-012](docs/src/content/docs/specs/openapm-v0.1.md#req-lk-012) | MUST | 5.2 | consumer | skipped | 1 |
| [req-lk-013](docs/src/content/docs/specs/openapm-v0.1.md#req-lk-013) | MUST | 5.2 | consumer | skipped | 1 |
| [req-lk-014](docs/src/content/docs/specs/openapm-v0.1.md#req-lk-014) | MUST | 5.2 | consumer | skipped | 1 |
| [req-lk-015](docs/src/content/docs/specs/openapm-v0.1.md#req-lk-015) | MUST | 5.6.4 | consumer | skipped | 1 |
| [req-lk-016](docs/src/content/docs/specs/openapm-v0.1.md#req-lk-016) | MUST | 5.2 | consumer | skipped | 1 |
| [req-lk-017](docs/src/content/docs/specs/openapm-v0.1.md#req-lk-017) | MUST | 5.2 | consumer | skipped | 1 |
| [req-lk-018](docs/src/content/docs/specs/openapm-v0.1.md#req-lk-018) | SHOULD | 5.5 | consumer | skipped | 1 |
| [req-mf-001](docs/src/content/docs/specs/openapm-v0.1.md#req-mf-001) | MUST | 4.1 | producer | active | 1 |
| [req-mf-002](docs/src/content/docs/specs/openapm-v0.1.md#req-mf-002) | MUST | 4.1 | producer | active | 1 |
| [req-mf-003](docs/src/content/docs/specs/openapm-v0.1.md#req-mf-003) | MUST | 4.1 | producer | active | 1 |
| [req-mf-004](docs/src/content/docs/specs/openapm-v0.1.md#req-mf-004) | SHOULD | 4.1 | producer | skipped | 1 |
| [req-mf-005](docs/src/content/docs/specs/openapm-v0.1.md#req-mf-005) | MUST | 4.2.1 | producer | active | 1 |
| [req-mf-006](docs/src/content/docs/specs/openapm-v0.1.md#req-mf-006) | MUST | 4.1 | consumer | active | 1 |
| [req-mf-007](docs/src/content/docs/specs/openapm-v0.1.md#req-mf-007) | MUST | 4.3.1 | consumer | skipped | 1 |
| [req-mf-008](docs/src/content/docs/specs/openapm-v0.1.md#req-mf-008) | MUST | 4.3.3 | consumer | skipped | 1 |
| [req-mf-009](docs/src/content/docs/specs/openapm-v0.1.md#req-mf-009) | MUST | 4.3.4 | consumer | skipped | 1 |
| [req-mf-010](docs/src/content/docs/specs/openapm-v0.1.md#req-mf-010) | MUST | 4.3.2 | consumer | skipped | 1 |
| [req-mf-011](docs/src/content/docs/specs/openapm-v0.1.md#req-mf-011) | MUST | 4.3.2 | consumer | skipped | 1 |
| [req-mf-012](docs/src/content/docs/specs/openapm-v0.1.md#req-mf-012) | MUST | 4.3.6 | consumer | skipped | 1 |
| [req-mf-013](docs/src/content/docs/specs/openapm-v0.1.md#req-mf-013) | MUST | 4.5 | consumer | skipped | 1 |
| [req-mf-014](docs/src/content/docs/specs/openapm-v0.1.md#req-mf-014) | MUST | 4.2.3 | producer | skipped | 1 |
| [req-mf-015](docs/src/content/docs/specs/openapm-v0.1.md#req-mf-015) | MUST | 4.2.3 | producer | skipped | 1 |
| [req-mf-016](docs/src/content/docs/specs/openapm-v0.1.md#req-mf-016) | MUST | 4.3.5 | consumer | skipped | 1 |
| [req-mf-017](docs/src/content/docs/specs/openapm-v0.1.md#req-mf-017) | MUST | 4.7 | producer | skipped | 1 |
| [req-mf-018](docs/src/content/docs/specs/openapm-v0.1.md#req-mf-018) | MUST | 4.6.1 | consumer | skipped | 1 |
| [req-mf-019](docs/src/content/docs/specs/openapm-v0.1.md#req-mf-019) | MUST | 4.2.4 | consumer | active | 1 |
| [req-mf-020](docs/src/content/docs/specs/openapm-v0.1.md#req-mf-020) | MUST | 4.1 | consumer | skipped | 1 |
| [req-mf-021](docs/src/content/docs/specs/openapm-v0.1.md#req-mf-021) | MUST | 4.8 | producer | skipped | 1 |
| [req-pl-001](docs/src/content/docs/specs/openapm-v0.1.md#req-pl-001) | MUST | 6.1 | governance | active | 1 |
| [req-pl-002](docs/src/content/docs/specs/openapm-v0.1.md#req-pl-002) | MUST | 6.2 | governance | active | 1 |
| [req-pl-003](docs/src/content/docs/specs/openapm-v0.1.md#req-pl-003) | MUST | 6.4 | governance | active | 1 |
| [req-pl-004](docs/src/content/docs/specs/openapm-v0.1.md#req-pl-004) | MUST | 6.4 | governance | active | 1 |
| [req-pl-005](docs/src/content/docs/specs/openapm-v0.1.md#req-pl-005) | MUST | 6.5 | governance | skipped | 1 |
| [req-pl-006](docs/src/content/docs/specs/openapm-v0.1.md#req-pl-006) | MUST | 6.4 | governance | skipped | 1 |
| [req-pl-007](docs/src/content/docs/specs/openapm-v0.1.md#req-pl-007) | MUST | 6.3.1 | governance | skipped | 1 |
| [req-pl-008](docs/src/content/docs/specs/openapm-v0.1.md#req-pl-008) | MUST | 6.3.1 | governance | skipped | 1 |
| [req-pl-009](docs/src/content/docs/specs/openapm-v0.1.md#req-pl-009) | MUST | 6.6 | governance | skipped | 1 |
| [req-pl-010](docs/src/content/docs/specs/openapm-v0.1.md#req-pl-010) | MUST | 6.2 | governance | skipped | 1 |
| [req-pl-011](docs/src/content/docs/specs/openapm-v0.1.md#req-pl-011) | MUST | 6.1.1 | governance | skipped | 1 |
| [req-pl-012](docs/src/content/docs/specs/openapm-v0.1.md#req-pl-012) | MUST | 6.1.1 | governance | skipped | 1 |
| [req-pr-001](docs/src/content/docs/specs/openapm-v0.1.md#req-pr-001) | MUST | 8.2 | consumer | skipped | 1 |
| [req-pr-002](docs/src/content/docs/specs/openapm-v0.1.md#req-pr-002) | MUST | 8.3 | consumer | skipped | 1 |
| [req-pr-003](docs/src/content/docs/specs/openapm-v0.1.md#req-pr-003) | MUST | 8.3 | consumer | skipped | 1 |
| [req-pr-004](docs/src/content/docs/specs/openapm-v0.1.md#req-pr-004) | MUST | 7.8 | producer | skipped | 1 |
| [req-pr-005](docs/src/content/docs/specs/openapm-v0.1.md#req-pr-005) | SHOULD | 7.8 | producer | skipped | 1 |
| [req-rg-001](docs/src/content/docs/specs/openapm-v0.1.md#req-rg-001) | MUST | 11.3.3 | registry | skipped | 1 |
| [req-rs-001](docs/src/content/docs/specs/openapm-v0.1.md#req-rs-001) | MUST | 7.2 | consumer | skipped | 1 |
| [req-rs-002](docs/src/content/docs/specs/openapm-v0.1.md#req-rs-002) | MUST | 7.3 | consumer | skipped | 1 |
| [req-rs-003](docs/src/content/docs/specs/openapm-v0.1.md#req-rs-003) | MUST | 7.3 | consumer | skipped | 1 |
| [req-rs-004](docs/src/content/docs/specs/openapm-v0.1.md#req-rs-004) | MUST | 7.5 | consumer | skipped | 1 |
| [req-rs-005](docs/src/content/docs/specs/openapm-v0.1.md#req-rs-005) | MUST | 7.6 | consumer | skipped | 1 |
| [req-rs-006](docs/src/content/docs/specs/openapm-v0.1.md#req-rs-006) | MUST | 7.2 | consumer | skipped | 1 |
| [req-rs-007](docs/src/content/docs/specs/openapm-v0.1.md#req-rs-007) | MUST | 7.3 | consumer | active | 1 |
| [req-rs-008](docs/src/content/docs/specs/openapm-v0.1.md#req-rs-008) | MUST | 7.1 | consumer | skipped | 1 |
| [req-rs-009](docs/src/content/docs/specs/openapm-v0.1.md#req-rs-009) | MUST | 7.5.1 | consumer | skipped | 1 |
| [req-rs-010](docs/src/content/docs/specs/openapm-v0.1.md#req-rs-010) | MUST | 7.2 | consumer | skipped | 1 |
| [req-rs-011](docs/src/content/docs/specs/openapm-v0.1.md#req-rs-011) | MUST | 7.7 | consumer | skipped | 1 |
| [req-rs-012](docs/src/content/docs/specs/openapm-v0.1.md#req-rs-012) | MUST | 7.7 | consumer | skipped | 1 |
| [req-rs-013](docs/src/content/docs/specs/openapm-v0.1.md#req-rs-013) | MUST | 7.2 | consumer | skipped | 1 |
| [req-rs-014](docs/src/content/docs/specs/openapm-v0.1.md#req-rs-014) | MUST | 7.3.1 | consumer | skipped | 1 |
| [req-sc-001](docs/src/content/docs/specs/openapm-v0.1.md#req-sc-001) | MUST | 10.4 | consumer | skipped | 1 |
| [req-sc-002](docs/src/content/docs/specs/openapm-v0.1.md#req-sc-002) | MUST | 10.9 | consumer | skipped | 1 |
| [req-sc-003](docs/src/content/docs/specs/openapm-v0.1.md#req-sc-003) | MUST | 10.3 | consumer | skipped | 1 |
| [req-sc-004](docs/src/content/docs/specs/openapm-v0.1.md#req-sc-004) | MUST | 10.5 | consumer | skipped | 1 |
| [req-sc-005](docs/src/content/docs/specs/openapm-v0.1.md#req-sc-005) | MUST | 10.3 | consumer | skipped | 1 |
| [req-sc-006](docs/src/content/docs/specs/openapm-v0.1.md#req-sc-006) | MUST | 4.2.3 | consumer | skipped | 1 |
| [req-sc-007](docs/src/content/docs/specs/openapm-v0.1.md#req-sc-007) | MUST | 10.3 | consumer | skipped | 1 |
| [req-sc-008](docs/src/content/docs/specs/openapm-v0.1.md#req-sc-008) | SHOULD | 10.3 | consumer | skipped | 1 |
| [req-tg-001](docs/src/content/docs/specs/openapm-v0.1.md#req-tg-001) | MUST | 8.4 | consumer | skipped | 1 |
| [req-tg-002](docs/src/content/docs/specs/openapm-v0.1.md#req-tg-002) | MUST | 8.5 | consumer | skipped | 1 |
| [req-tg-003](docs/src/content/docs/specs/openapm-v0.1.md#req-tg-003) | MUST | 8.5 | consumer | skipped | 1 |
| [req-tg-004](docs/src/content/docs/specs/openapm-v0.1.md#req-tg-004) | MUST | 4.2.1 | consumer | skipped | 1 |

## Waivers

### req-cf-001
- Covered by test_round_trip.py stage-2 byte-equality assertion. Imported here for marker coverage in this cluster.

### req-cf-002
- CONFORMANCE.{md,json} not yet generated in this checkout. Run `uv run python -m tests.spec_conformance.gen_statement` to regenerate; CI enforces the diff.

### req-lk-005
- Resolved-field detail varies by source kind; covered by V2 fixture.

### req-lk-007
- SHOULD requirement; structural.

### req-lk-009
- Registry-URL recording; structural, covered by V2 fixture shape.

### req-lk-010
- Registry-digest detail; covered structurally by integrity cluster.

### req-lk-012
- Active fail-closed extract test deferred. Stub fixture path: tests/fixtures/spec-conformance/integrity/canonical-tree/. v0.1.2 hand-computed tree_sha256 fixture lands here.

### req-lk-013
- Active fail-closed extract test deferred. Stub fixture path: tests/fixtures/spec-conformance/integrity/hash-mismatch.frozen.yaml + archive. v0.1.2 lands the paired archive + hash-mismatch oracle.

### req-lk-014
- Negative-algorithm test; deferred to v0.1.2 fixture expansion.

### req-lk-015
- Canonical-tree invariant; partners with req-lk-012 fixture set. v0.1.2 lands hand-computed reference.

### req-lk-016
- Bare-hex reader tolerance fixture deferred. Stub fixture: tests/fixtures/spec-conformance/integrity/bare-hex-reader.yaml.

### req-lk-017
- Active deployed-file-hash mismatch oracle deferred. Stub fixture: tests/fixtures/spec-conformance/integrity/deployed-file-mismatch.yaml.

### req-lk-018
- SHOULD requirement; structural.

### req-mf-004
- SHOULD requirement; informational. Producers may omit description; active negative-policy test deferred to v0.1.2.

### req-mf-005
- Seed fixture has no dependencies block to validate shape.

### req-mf-007
- Resolver integration test; covered structurally by req-rs-001 cluster.

### req-mf-008
- Pin-handling integration; see req-rs-006.

### req-mf-009
- Commit-pin integration; see req-rs-006.

### req-mf-010
- Short-form `apm:` resolution shape covered structurally elsewhere.

### req-mf-011
- Table-form `apm:` resolution shape covered structurally elsewhere.

### req-mf-012
- Negative test requires constructing a malformed manifest beyond the seed fixture set. Deferred to v0.1.2 fixture expansion.

### req-mf-013
- Local-path source integration; deferred to v0.1.2 fixture expansion.

### req-mf-014
- Producer-side primitive listing; see req-pr-004/005 cluster.

### req-mf-015
- Producer-side path constraint; covered structurally by primitive cluster.

### req-mf-016
- Absolute-path rejection; deferred to v0.1.2 fixture expansion.

### req-mf-017
- Repo-root constraint; structural, no apm_cli surface to assert against.

### req-mf-018
- Resolution-order detail; integration test deferred to v0.1.2.

### req-mf-020
- Round-trip cluster; see req-cf-001 (round-trip fixed-point).

### req-mf-021
- Producer publish surface; deferred to v0.2 producer harness.

### req-pl-004
- apm_cli policy-cycle detector wire-up is the v0.1.2 follow-up. Cycle fixture is in place; oracle binding deferred.

### req-pl-005
- Rule-set shape; covered structurally by valid-extends fixture.

### req-pl-006
- Path resolution detail; deferred to v0.1.2 fixture expansion.

### req-pl-007
- Action enum coverage; deferred to v0.1.2 fixture expansion.

### req-pl-008
- Action enum coverage; deferred to v0.1.2 fixture expansion.

### req-pl-009
- Evaluator behaviour; integration test deferred to v0.1.2.

### req-pl-010
- v0.1 policy seed fixtures do not yet carry an apiVersion field; deferred.

### req-pl-011
- Default-allow shape; structural.

### req-pl-012
- Default-deny shape; structural.

### req-pr-001
- Primitive loading integration; deferred to v0.1.2.

### req-pr-002
- Namespacing rule; structural.

### req-pr-003
- Collision-fail-closed; deferred to v0.1.2.

### req-pr-004
- Producer-publish surface; deferred to v0.2.

### req-pr-005
- SHOULD requirement; structural.

### req-rg-001
- v0.1 registry class ships only this trust-anchor MUST; no wire surface to exercise. Tracked at sec.11.3.3; v0.2 expands.

### req-rs-001
- Determinism integration; deferred to v0.1.2.

### req-rs-002
- Resolution -> lockfile integration; deferred to v0.1.2.

### req-rs-003
- Pin-honour integration; deferred to v0.1.2.

### req-rs-004
- Provenance binding; structural.

### req-rs-005
- Negative resolution path; deferred to v0.1.2 fixture expansion.

### req-rs-006
- Commit-pin integration; deferred to v0.1.2.

### req-rs-008
- Range-grammar binding; covered structurally by req-rs-007 oracle.

### req-rs-009
- Range-grammar binding; covered structurally by req-rs-007 oracle.

### req-rs-010
- Exact-pin integration; deferred to v0.1.2.

### req-rs-011
- Source-URL recording; covered structurally by lockfile cluster.

### req-rs-012
- Resolved-ref recording; covered structurally by lockfile cluster.

### req-rs-013
- Ambiguity-fail-closed; deferred to v0.1.2.

### req-rs-014
- Prerelease semantics; covered structurally by req-rs-007 oracle shape.

### req-sc-001
- Scheme registry test; structural, no fixture surface in v0.1.

### req-sc-002
- Scheme registry test; structural, no fixture surface in v0.1.

### req-sc-003
- Scheme registry test; structural, no fixture surface in v0.1.

### req-sc-004
- Scheme registry test; structural, no fixture surface in v0.1.

### req-sc-005
- Scheme registry test; structural, no fixture surface in v0.1.

### req-sc-006
- Scheme normalisation; deferred to v0.1.2 fixture expansion.

### req-sc-007
- Negative-scheme test; deferred to v0.1.2 fixture expansion.

### req-sc-008
- SHOULD requirement; structural.

### req-tg-001
- Tag pin resolution; covered structurally by req-rs cluster.

### req-tg-002
- Immutability is enforced post-resolution via lockfile hash, see req-lk-013.

### req-tg-003
- Lockfile-tag binding; see req-lk-013/017.

### req-tg-004
- Scheme-routing for tag fetch; structural.

