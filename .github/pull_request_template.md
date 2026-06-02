## Description

Brief description of changes and motivation.

Fixes # (issue)

## Type of change

- [ ] Bug fix
- [ ] New feature
- [ ] Documentation
- [ ] Maintenance / refactor

## Testing

- [ ] Tested locally
- [ ] All existing tests pass
- [ ] Added tests for new functionality (if applicable)

## Spec conformance (OpenAPM v0.1)

If this PR changes behaviour that an OpenAPM v0.1 `req-XXX` covers,
confirm the three-step ritual (see CONTRIBUTING.md "Adding or
changing a normative requirement"):

- [ ] Spec edit: `docs/src/content/docs/specs/openapm-v0.1.md` updated
      (new/changed `<a id="req-XXX"></a>` anchor + prose + Appendix C
      row).
- [ ] Manifest edit: `docs/src/content/docs/specs/manifests/openapm-v0.1.requirements.yml`
      updated.
- [ ] Test edit: a `@pytest.mark.req("req-XXX")` test under
      `tests/spec_conformance/` added or extended.
- [ ] `CONFORMANCE.{md,json}` regenerated via
      `uv run --extra dev python -m tests.spec_conformance.gen_statement`
      and committed.
- [ ] N/A -- this PR does not change OpenAPM-observable behaviour.
