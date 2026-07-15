# OpenAPM Conformance Statement -- v0.1.1

Generator: gen_statement.py v1.
Spec: [docs/src/content/docs/specs/openapm-v0.1.md](docs/src/content/docs/specs/openapm-v0.1.md)

This file is generated. Do NOT edit by hand. Run
`uv run python -m tests.spec_conformance.gen_statement` to regenerate.

## Honesty contract

There is NO automated CI detector for spec-vs-behaviour drift beyond the four sets enforced by `orphan_check.py`: spec anchors, manifest entries, Appendix C rows, and `@pytest.mark.req` markers. A requirement marked `status=active` is exercised by at least one assertion. A requirement marked `status=skipped` carries a written waiver below; this is debt, not coverage. A requirement with `status=xfail` is asserted-but-known-broken.

## Conformance classes

All four conformance classes (Producer, Consumer, Registry, Governance) carry active coverage in this statement. The Registry class is exercised via the trust-anchor invariant test in `tests/spec_conformance/test_registry_reqs.py`, which hashes the committed Registry-archive fixture and asserts equality with the digest the paired lockfile advertises (sec.11.3.3, req-rg-001).

## Coverage summary

| Class | Active | Skipped | Xfail | Unbound |
|-------|-------:|--------:|------:|--------:|
| Producer | 12 | 0 | 0 | 0 |
| Consumer | 71 | 1 | 0 | 0 |
| Registry | 1 | 0 | 0 | 0 |
| Governance | 15 | 0 | 0 | 0 |

## Per-requirement coverage

| Req ID | Keyword | Sec | Class | Status | Tests |
|--------|---------|----:|-------|--------|------:|
| [req-cf-001](docs/src/content/docs/specs/openapm-v0.1.md#req-cf-001) | MUST | 12.5 | consumer | active | 6 |
| [req-cf-002](docs/src/content/docs/specs/openapm-v0.1.md#req-cf-002) | MUST | 12.3 | consumer | active | 1 |
| [req-ext-001](docs/src/content/docs/specs/openapm-v0.1.md#req-ext-001) | MUST | 4.1 | consumer | active | 1 |
| [req-ext-002](docs/src/content/docs/specs/openapm-v0.1.md#req-ext-002) | MUST | 4.1 | producer | active | 1 |
| [req-lk-001](docs/src/content/docs/specs/openapm-v0.1.md#req-lk-001) | MUST | 5.1 | consumer | active | 1 |
| [req-lk-002](docs/src/content/docs/specs/openapm-v0.1.md#req-lk-002) | MUST | 5.4 | consumer | active | 1 |
| [req-lk-003](docs/src/content/docs/specs/openapm-v0.1.md#req-lk-003) | MUST | 5.2 | consumer | active | 2 |
| [req-lk-004](docs/src/content/docs/specs/openapm-v0.1.md#req-lk-004) | MUST | 5.4 | consumer | active | 1 |
| [req-lk-005](docs/src/content/docs/specs/openapm-v0.1.md#req-lk-005) | MUST | 5.5 | consumer | active | 1 |
| [req-lk-006](docs/src/content/docs/specs/openapm-v0.1.md#req-lk-006) | MUST | 5.5 | consumer | active | 1 |
| [req-lk-007](docs/src/content/docs/specs/openapm-v0.1.md#req-lk-007) | SHOULD | 5.5 | consumer | active | 1 |
| [req-lk-008](docs/src/content/docs/specs/openapm-v0.1.md#req-lk-008) | MUST | 5.6 | consumer | active | 1 |
| [req-lk-009](docs/src/content/docs/specs/openapm-v0.1.md#req-lk-009) | MUST | 5.6 | consumer | active | 1 |
| [req-lk-010](docs/src/content/docs/specs/openapm-v0.1.md#req-lk-010) | MUST | 5.6 | consumer | active | 1 |
| [req-lk-011](docs/src/content/docs/specs/openapm-v0.1.md#req-lk-011) | MUST | 5.2 | consumer | active | 1 |
| [req-lk-012](docs/src/content/docs/specs/openapm-v0.1.md#req-lk-012) | MUST | 5.2 | consumer | active | 1 |
| [req-lk-013](docs/src/content/docs/specs/openapm-v0.1.md#req-lk-013) | MUST | 5.2 | consumer | active | 1 |
| [req-lk-014](docs/src/content/docs/specs/openapm-v0.1.md#req-lk-014) | MUST | 5.2 | consumer | active | 1 |
| [req-lk-015](docs/src/content/docs/specs/openapm-v0.1.md#req-lk-015) | MUST | 5.6.4 | consumer | active | 1 |
| [req-lk-016](docs/src/content/docs/specs/openapm-v0.1.md#req-lk-016) | MUST | 5.2 | consumer | active | 1 |
| [req-lk-017](docs/src/content/docs/specs/openapm-v0.1.md#req-lk-017) | MUST | 5.2 | consumer | active | 1 |
| [req-lk-018](docs/src/content/docs/specs/openapm-v0.1.md#req-lk-018) | SHOULD | 5.5 | consumer | active | 1 |
| [req-lk-019](docs/src/content/docs/specs/openapm-v0.1.md#req-lk-019) | MUST | 5.2 | consumer | active | 1 |
| [req-lk-020](docs/src/content/docs/specs/openapm-v0.1.md#req-lk-020) | MUST | 5.2 | consumer | active | 1 |
| [req-mf-001](docs/src/content/docs/specs/openapm-v0.1.md#req-mf-001) | MUST | 4.1 | producer | active | 1 |
| [req-mf-002](docs/src/content/docs/specs/openapm-v0.1.md#req-mf-002) | MUST | 4.1 | producer | active | 1 |
| [req-mf-003](docs/src/content/docs/specs/openapm-v0.1.md#req-mf-003) | MUST | 4.1 | producer | active | 1 |
| [req-mf-004](docs/src/content/docs/specs/openapm-v0.1.md#req-mf-004) | SHOULD | 4.1 | producer | active | 1 |
| [req-mf-005](docs/src/content/docs/specs/openapm-v0.1.md#req-mf-005) | MUST | 4.2.1 | producer | active | 1 |
| [req-mf-006](docs/src/content/docs/specs/openapm-v0.1.md#req-mf-006) | MUST | 4.1 | consumer | active | 1 |
| [req-mf-007](docs/src/content/docs/specs/openapm-v0.1.md#req-mf-007) | MUST | 4.3.1 | consumer | active | 1 |
| [req-mf-008](docs/src/content/docs/specs/openapm-v0.1.md#req-mf-008) | MUST | 4.3.3 | consumer | active | 1 |
| [req-mf-009](docs/src/content/docs/specs/openapm-v0.1.md#req-mf-009) | MUST | 4.3.4 | consumer | active | 1 |
| [req-mf-010](docs/src/content/docs/specs/openapm-v0.1.md#req-mf-010) | MUST | 4.3.2 | consumer | active | 1 |
| [req-mf-011](docs/src/content/docs/specs/openapm-v0.1.md#req-mf-011) | MUST | 4.3.2 | consumer | active | 1 |
| [req-mf-012](docs/src/content/docs/specs/openapm-v0.1.md#req-mf-012) | MUST | 4.3.6 | consumer | active | 1 |
| [req-mf-013](docs/src/content/docs/specs/openapm-v0.1.md#req-mf-013) | MUST | 4.5 | consumer | active | 1 |
| [req-mf-014](docs/src/content/docs/specs/openapm-v0.1.md#req-mf-014) | MUST | 4.2.3 | producer | active | 1 |
| [req-mf-015](docs/src/content/docs/specs/openapm-v0.1.md#req-mf-015) | MUST | 4.2.3 | producer | active | 1 |
| [req-mf-016](docs/src/content/docs/specs/openapm-v0.1.md#req-mf-016) | MUST | 4.3.5 | consumer | skipped | 1 |
| [req-mf-017](docs/src/content/docs/specs/openapm-v0.1.md#req-mf-017) | MUST | 4.7 | producer | active | 1 |
| [req-mf-018](docs/src/content/docs/specs/openapm-v0.1.md#req-mf-018) | MUST | 4.6.1 | consumer | active | 1 |
| [req-mf-019](docs/src/content/docs/specs/openapm-v0.1.md#req-mf-019) | MUST | 4.2.4 | consumer | active | 1 |
| [req-mf-020](docs/src/content/docs/specs/openapm-v0.1.md#req-mf-020) | MUST | 4.1 | consumer | active | 1 |
| [req-mf-021](docs/src/content/docs/specs/openapm-v0.1.md#req-mf-021) | MUST | 4.8 | producer | active | 1 |
| [req-pl-001](docs/src/content/docs/specs/openapm-v0.1.md#req-pl-001) | MUST | 6.1 | governance | active | 1 |
| [req-pl-002](docs/src/content/docs/specs/openapm-v0.1.md#req-pl-002) | MUST | 6.2 | governance | active | 1 |
| [req-pl-003](docs/src/content/docs/specs/openapm-v0.1.md#req-pl-003) | MUST | 6.4 | governance | active | 1 |
| [req-pl-004](docs/src/content/docs/specs/openapm-v0.1.md#req-pl-004) | MUST | 6.4 | governance | active | 1 |
| [req-pl-005](docs/src/content/docs/specs/openapm-v0.1.md#req-pl-005) | MUST | 6.5 | governance | active | 1 |
| [req-pl-006](docs/src/content/docs/specs/openapm-v0.1.md#req-pl-006) | MUST | 6.4 | governance | active | 1 |
| [req-pl-007](docs/src/content/docs/specs/openapm-v0.1.md#req-pl-007) | MUST | 6.3.1 | governance | active | 1 |
| [req-pl-008](docs/src/content/docs/specs/openapm-v0.1.md#req-pl-008) | MUST | 6.3.1 | governance | active | 1 |
| [req-pl-009](docs/src/content/docs/specs/openapm-v0.1.md#req-pl-009) | MUST | 6.6 | governance | active | 1 |
| [req-pl-010](docs/src/content/docs/specs/openapm-v0.1.md#req-pl-010) | MUST | 6.2 | governance | active | 1 |
| [req-pl-011](docs/src/content/docs/specs/openapm-v0.1.md#req-pl-011) | MUST | 6.1.1 | governance | active | 1 |
| [req-pl-012](docs/src/content/docs/specs/openapm-v0.1.md#req-pl-012) | MUST | 6.1.1 | governance | active | 1 |
| [req-pl-013](docs/src/content/docs/specs/openapm-v0.1.md#req-pl-013) | MUST | 6.8 | governance | active | 1 |
| [req-pl-014](docs/src/content/docs/specs/openapm-v0.1.md#req-pl-014) | MUST | 6.8 | governance | active | 1 |
| [req-pl-015](docs/src/content/docs/specs/openapm-v0.1.md#req-pl-015) | MUST | 6.3.5 | governance | active | 1 |
| [req-pr-001](docs/src/content/docs/specs/openapm-v0.1.md#req-pr-001) | MUST | 8.2 | consumer | active | 1 |
| [req-pr-002](docs/src/content/docs/specs/openapm-v0.1.md#req-pr-002) | MUST | 8.3 | consumer | active | 1 |
| [req-pr-003](docs/src/content/docs/specs/openapm-v0.1.md#req-pr-003) | MUST | 8.3 | consumer | active | 1 |
| [req-pr-004](docs/src/content/docs/specs/openapm-v0.1.md#req-pr-004) | MUST | 7.8 | producer | active | 10 |
| [req-pr-005](docs/src/content/docs/specs/openapm-v0.1.md#req-pr-005) | SHOULD | 7.8 | producer | active | 1 |
| [req-rg-001](docs/src/content/docs/specs/openapm-v0.1.md#req-rg-001) | MUST | 11.3.3 | registry | active | 1 |
| [req-rs-001](docs/src/content/docs/specs/openapm-v0.1.md#req-rs-001) | MUST | 7.2 | consumer | active | 1 |
| [req-rs-002](docs/src/content/docs/specs/openapm-v0.1.md#req-rs-002) | MUST | 7.3 | consumer | active | 1 |
| [req-rs-003](docs/src/content/docs/specs/openapm-v0.1.md#req-rs-003) | MUST | 7.3 | consumer | active | 1 |
| [req-rs-004](docs/src/content/docs/specs/openapm-v0.1.md#req-rs-004) | MUST | 7.5 | consumer | active | 1 |
| [req-rs-005](docs/src/content/docs/specs/openapm-v0.1.md#req-rs-005) | MUST | 7.6 | consumer | active | 1 |
| [req-rs-006](docs/src/content/docs/specs/openapm-v0.1.md#req-rs-006) | MUST | 7.2 | consumer | active | 1 |
| [req-rs-007](docs/src/content/docs/specs/openapm-v0.1.md#req-rs-007) | MUST | 7.3 | consumer | active | 1 |
| [req-rs-008](docs/src/content/docs/specs/openapm-v0.1.md#req-rs-008) | MUST | 7.1 | consumer | active | 7 |
| [req-rs-009](docs/src/content/docs/specs/openapm-v0.1.md#req-rs-009) | MUST | 7.5.1 | consumer | active | 1 |
| [req-rs-010](docs/src/content/docs/specs/openapm-v0.1.md#req-rs-010) | MUST | 7.2 | consumer | active | 1 |
| [req-rs-011](docs/src/content/docs/specs/openapm-v0.1.md#req-rs-011) | MUST | 7.7 | consumer | active | 1 |
| [req-rs-012](docs/src/content/docs/specs/openapm-v0.1.md#req-rs-012) | MUST | 7.7 | consumer | active | 1 |
| [req-rs-013](docs/src/content/docs/specs/openapm-v0.1.md#req-rs-013) | MUST | 7.2 | consumer | active | 1 |
| [req-rs-014](docs/src/content/docs/specs/openapm-v0.1.md#req-rs-014) | MUST | 7.3.1 | consumer | active | 1 |
| [req-rs-015](docs/src/content/docs/specs/openapm-v0.1.md#req-rs-015) | MUST | 7.5 | consumer | active | 1 |
| [req-rs-016](docs/src/content/docs/specs/openapm-v0.1.md#req-rs-016) | MUST | 7.2 | consumer | active | 7 |
| [req-sc-001](docs/src/content/docs/specs/openapm-v0.1.md#req-sc-001) | MUST | 10.4 | consumer | active | 1 |
| [req-sc-002](docs/src/content/docs/specs/openapm-v0.1.md#req-sc-002) | MUST | 10.9 | consumer | active | 1 |
| [req-sc-003](docs/src/content/docs/specs/openapm-v0.1.md#req-sc-003) | MUST | 10.3 | consumer | active | 1 |
| [req-sc-004](docs/src/content/docs/specs/openapm-v0.1.md#req-sc-004) | MUST | 10.5 | consumer | active | 1 |
| [req-sc-005](docs/src/content/docs/specs/openapm-v0.1.md#req-sc-005) | MUST | 10.3 | consumer | active | 1 |
| [req-sc-006](docs/src/content/docs/specs/openapm-v0.1.md#req-sc-006) | MUST | 4.2.3 | consumer | active | 1 |
| [req-sc-007](docs/src/content/docs/specs/openapm-v0.1.md#req-sc-007) | MUST | 10.3 | consumer | active | 1 |
| [req-sc-008](docs/src/content/docs/specs/openapm-v0.1.md#req-sc-008) | SHOULD | 10.3 | consumer | active | 1 |
| [req-sc-009](docs/src/content/docs/specs/openapm-v0.1.md#req-sc-009) | MUST | 10.13 | consumer | active | 1 |
| [req-sc-010](docs/src/content/docs/specs/openapm-v0.1.md#req-sc-010) | MUST | 10.13 | consumer | active | 1 |
| [req-sc-011](docs/src/content/docs/specs/openapm-v0.1.md#req-sc-011) | MUST | 10.14 | consumer | active | 1 |
| [req-sc-012](docs/src/content/docs/specs/openapm-v0.1.md#req-sc-012) | MUST | 10.14 | consumer | active | 1 |
| [req-tg-001](docs/src/content/docs/specs/openapm-v0.1.md#req-tg-001) | MUST | 8.4 | consumer | active | 1 |
| [req-tg-002](docs/src/content/docs/specs/openapm-v0.1.md#req-tg-002) | MUST | 8.5 | consumer | active | 1 |
| [req-tg-003](docs/src/content/docs/specs/openapm-v0.1.md#req-tg-003) | MUST | 8.5 | consumer | active | 1 |
| [req-tg-004](docs/src/content/docs/specs/openapm-v0.1.md#req-tg-004) | MUST | 4.2.1 | consumer | active | 1 |
| [req-tg-005](docs/src/content/docs/specs/openapm-v0.1.md#req-tg-005) | MUST | 8.5 | consumer | active | 1 |
| [req-tg-006](docs/src/content/docs/specs/openapm-v0.1.md#req-tg-006) | MUST | 8.5.1 | consumer | active | 1 |

## Waivers

### req-cf-002
- CONFORMANCE.{md,json} not yet generated in this checkout. Run `uv run python -m tests.spec_conformance.gen_statement`.

### req-lk-018
- Publish-timestamp recording is a publisher-side SHOULD that requires registry interaction to exercise end-to-end. The schema affordance (generated_at) is asserted above; full publisher coverage requires the registry wire conformance module which is not in v0.1 scope.

### req-mf-016
- Path-shape negative test requires apm_cli's path-policy loader to be invokable from the test harness; the JSON Schema currently models `path` as a free-form string. Tracked as a follow-up: tighten the schema to forbid leading `/` and document the absolute-path rejection in the schema additionalProperties.

