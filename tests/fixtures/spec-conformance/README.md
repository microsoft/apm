# OpenAPM v0.1 conformance fixtures (seed)

This directory holds seed conformance fixtures referenced normatively
by [OpenAPM v0.1 Section 12.4](../../../docs/src/content/docs/specs/openapm-v0.1.md#124-fixture-layout-informative).

Fixtures are organised by document kind and cite the `req-XXX` identifiers
they exercise in a header comment.

## Layout

| Directory                                | Purpose                                                     |
|------------------------------------------|-------------------------------------------------------------|
| `manifest/`                              | `apm.yml` fixtures (valid / invalid / round-trip).          |
| `lockfile/`                              | `apm.lock.yaml` fixtures including v1, v2, round-trip.      |
| `policy/`                                | `apm-policy.yml` fixtures (valid + invalid).                |
| `resolution/semver-dialect.json`         | Canonical semver-range -> tag-set table per req-rs-007.     |

## Binding to the spec

- Each fixture file starts with a header comment listing the `req-XXX`
  identifiers it exercises.
- `tests/fixtures/spec-conformance/resolution/semver-dialect.json` is the
  reference oracle for [req-rs-007](../../../docs/src/content/docs/specs/openapm-v0.1.md#req-rs-007)
  (semver dialect pin).
- This is a **seed** set. v0.2 expands the suite and wires CI binding to
  `MUST` (currently `RECOMMENDED` per [§12.3](../../../docs/src/content/docs/specs/openapm-v0.1.md#123-ci-binding)).

## Forward plan

A machine-readable requirements manifest (`tests/spec-conformance/requirements.yml`)
generated from Appendix C of the spec is reserved for v0.2 per
[§12.6](../../../docs/src/content/docs/specs/openapm-v0.1.md#126-machine-readable-conformance-requirements-reserved-for-v02).
