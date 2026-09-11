## Description

Brief description of changes and motivation.

## Issue and approved scope

Issue: # (required; use the canonical issue or a bounded child issue)

Human scope-approval comment: (link, state the trivial-documentation
standing-preapproval case, or indicate private security coordination without
disclosing report details)

Does this PR complete the issue, or what remains?

<!-- Use "Fixes #N" only for an issue this PR completes. A related issue,
     automated recommendation, or label alone is not scope approval.
     Routine automated maintenance needs a bounded, human-approved issue.
     Security changes may use private tracking: do not disclose report details.
     Existing PRs follow the transition guidance in CONTRIBUTING.md. -->

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
confirm the three-step ritual in the
[development guide](https://github.com/microsoft/apm/blob/main/docs/src/content/docs/contributing/development-guide.md#adding-or-changing-a-normative-requirement-openapm-v01):

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
