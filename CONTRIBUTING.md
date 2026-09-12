# Contributing to APM

Thank you for helping improve APM. Bug reports, reproductions, documentation,
reviews, and code are all valuable contributions. Please follow our
[Code of Conduct](CODE_OF_CONDUCT.md).

**Start with an issue, not an implementation.** For substantive changes, wait
for a responsible human maintainer to approve the scope before coding for
inclusion in APM. This avoids work the project cannot support or accept.
[GOVERNANCE.md](GOVERNANCE.md) names the maintainers and their decision areas.

## Find work or propose an idea

- **Find work:** look for [issues marked `help wanted`](https://github.com/microsoft/apm/issues?q=is%3Aissue+is%3Aopen+label%3A%22help+wanted%22).
  Read the issue's human approval and check that nobody is already working
  on it. `good first issue` identifies supported newcomer tasks when available.
- **Report a bug:** check existing issues and the current release, then use
  the [bug report template](https://github.com/microsoft/apm/issues/new?template=bug_report.md).
  Include a reproduction, expected behavior, version, and relevant logs.
- **Propose a change:** open an [issue](https://github.com/microsoft/apm/issues/new/choose)
  explaining the user problem, evidence, alternatives, and expected benefit.
  Proposals, investigations, and design discussion need no prior permission.

See [roadmap and release planning](GOVERNANCE.md#roadmap-and-release-planning)
for how proposals become priorities and release targets. Project rollout is
pending; roadmap placement is separate from human scope approval.

Do not report vulnerabilities or publish credentials in public issues or
PRs. Use the private reporting route in [SECURITY.md](SECURITY.md).

## Before you start implementation

A [responsible maintainer](GOVERNANCE.md#maintainers-and-scope) records approval
on the issue with:

- **Scope:** the bounded change the project welcomes.
- **Done when:** the acceptance criteria.
- **Out of scope:** important exclusions, if any.
- **Review contact:** a maintainer willing to support review.

`status/accepted` communicates this decision; the **human approval record is
authoritative**. A label, automated recommendation, milestone, or silence is
not approval. `needs-design` work needs an agreed design before implementation.
If no reviewer has capacity, maintainers may defer the proposal rather than
invite work they cannot support.

For accepted, unclaimed work, comment that you intend to work on it; you do
not need a second permission request. Check existing comments and linked PRs
to avoid duplicate work. Assignment records coordination, not permanent
ownership. If you need to pause, say so on the issue so someone else can help.
If the scope changes, discuss it before expanding the implementation.

### Scope record format

For new machine-readable evidence, the responsible human posts a **new,
unedited issue comment** in this format (one field per line):

```text
<!-- apm-scope:v1 -->
Decision: approve
Area: project
Scope: Describe the bounded change.
Done when: State the acceptance criteria.
Out of scope: Name important exclusions, or write None.
Review contact: @your-maintainer-login
```

Use `registry-public-api` instead of `project` only for that exact remit in
[the governance roster](GOVERNANCE.md#maintainers-and-scope). Prefer naming
yourself as review contact; obtain another maintainer's agreement before
committing their time. Link the issue and this comment in the PR body.
Ordinary issue references are valid for partial work; `Fixes` is not required.

To withdraw approval, post a new comment, preserving earlier history:

```text
<!-- apm-scope:v1 -->
Decision: withdraw
Area: project
Reason: Explain the scope or capacity change.
```

Do not edit or delete decisions to change their meaning. A missing referenced
comment or edited record needs fresh evidence, not fallback to an older
approval. Historical informal approvals need deliberate human confirmation;
the September 12 acceptance withdrawals are not automatically restored.

The eligibility report is **always neutral**, even when it finds a record.
It cannot judge whether code fits the issue or reconstruct a deleted withdrawal.
It does not replace human scope approval, review, or a fresh bounded human
confirmation before an automation run implements changes. API failures are
reported as unknown/error, never as approval.

Approval welcomes work within the agreed scope. It does **not** guarantee
merge, a particular implementation, or a release date. You may experiment in
your own fork without approval, but an unsolicited PR does not oblige
maintainers to review or adopt it.

### Small corrections

Typo fixes and broken documentation links have standing preapproval. Create
a short issue alongside the PR and link it; no separate approval wait is
needed. This exception does not cover code fixes, behavior changes, or
substantive documentation changes. If uncertain, ask on the issue.
The PR may include `<!-- apm-standing-preapproval: typo -->` or
`<!-- apm-standing-preapproval: broken-link -->`; this is a claim for human
review, not an automated exemption for a substantive change.

Maintainer-authored changes and automated PRs also need issue traceability.
Routine automation may use a bounded maintenance or release issue with
recorded human approval; it is not a blanket exception for unrelated work.
Security fixes may use private tracking and human coordination through the
security-reporting process; do not publish confidential approval links or
report details to satisfy the PR template.
Use `<!-- apm-private-tracking -->` to request confidential manual review
without a public tracking link. The advisory also routes security/dependency
submissions to manual review; it does not certify private approval or ask
authors to expose confidential evidence.

## Submit a reviewable PR

1. Fork and create a branch for the approved issue.
2. Keep the PR focused on one concern. Include relevant tests and update
   documentation for changed behavior.
3. Follow the [development guide](docs/src/content/docs/contributing/development-guide.md)
   for setup, local checks, and any surface-specific requirements.
4. In the PR template, link the canonical issue and human approval comment,
   or state the standing-preapproval case. Explain the change, evidence,
   and any remaining limitations.
5. Use `Fixes #N` only when the PR completes that issue. For partial work,
   reference the issue without closing it and describe what remains.

Maintain the same issue reference on stacked PRs, or link a bounded child
issue for each layer. Scope approval and PR review are separate decisions.
Required checks and maintainer review still apply before merge.

Maintainers check eligibility before detailed implementation review. Work
outside the agreed scope may be redirected or closed with a reason without
a full code review. A relevant issue link is not permission for unrelated
changes. Authors remain responsible for their submissions, including
agent-generated work.

### Agent tools are optional

You do not need an AI tool, a particular harness, or the repository's skills
to contribute. The [development guide](docs/src/content/docs/contributing/development-guide.md#optional-agent-tools)
explains the available tooling. Automated reviews are advisory: they do not
approve scope or replace human review. Relevant tests and a clear explanation
matter more than the tool used to produce them.

## What to expect from maintainers

Maintainers consider user need, project direction, maintenance cost, risk,
and review capacity. They may accept, request design or information, defer,
or decline with a reason. A milestone is a release target, not a prerequisite
for acceptance or a delivery guarantee.

Keep discussion on the issue or PR so others can follow the decision.
Maintainer availability varies; there is no guaranteed turnaround. If you
are waiting, a concise follow-up on the original thread is welcome. Waiting
on a maintainer is not contributor abandonment.

**Existing contributions:** older labels and automated comments may describe
a different process. Check for explicit human approval rather than treating
those signals as permission to start new work. Existing PRs will be considered
on their merits and may have wanted scope ratified now; they will not be
automatically rejected for lacking approval under a rule introduced later.

For responsibilities, progression, and how disagreements are resolved, see
[GOVERNANCE.md](GOVERNANCE.md).

## Development reference

The [development guide](docs/src/content/docs/contributing/development-guide.md)
contains environment setup, testing, linting, optional agent tooling, and
merge-queue guidance. Required technical checks are unchanged by this policy.

### Running the bounded mutation pilot

See the [mutation pilot instructions](docs/src/content/docs/contributing/development-guide.md#running-the-bounded-mutation-pilot),
including how to review survivor-baseline changes.

### How to add an experimental feature flag

See the [experimental flag recipe](docs/src/content/docs/contributing/development-guide.md#how-to-add-an-experimental-feature-flag).

## Spec amendment workflow

### Adding or changing a normative requirement (OpenAPM v0.1)

See the [normative requirement workflow](docs/src/content/docs/contributing/development-guide.md#adding-or-changing-a-normative-requirement-openapm-v01)
for the coupled specification, manifest, test, and conformance-statement edits.

## License

Contributions are licensed under the project's [MIT License](LICENSE).
