# APM project governance

APM is maintainer-led. Anyone may propose improvements and participate in
discussion; maintainers decide which work the project can support. This
document covers project responsibilities, not APM's enterprise policy engine.
The contribution process lives in [CONTRIBUTING.md](CONTRIBUTING.md).

## Maintainers and scope

| Maintainer | Role | Responsibility |
| --- | --- | --- |
| [Daniel Meppiel](https://github.com/danielmeppiel) | Lead and core maintainer | Project-wide scope approval, roadmap coordination, and resolution of unresolved direction disagreements. |
| [Sergio](https://github.com/sergio-sisternes-epam) | Core maintainer | Project-wide scope approval and technical maintenance, without requiring the lead to approve routine work again. |
| [Nadav](https://github.com/nadav-y) | Registry public API maintainer | Primary scope and technical owner for the APM registry public API. Core maintainers provide backup. |

Core maintainers have overlapping project-wide responsibility; the project
does not require an artificial split of every file into exclusive domains.
For registry public API work, involve Nadav first. His area does not
automatically include unrelated registry internals, broader CLI behavior,
or project-wide authentication policy.

For cross-area work, name one coordinating maintainer and consult affected
owners. When an owner is unavailable, a core maintainer may arrange qualified
backup and record the handoff on the issue. Ownership routes decisions; it
does not create a veto through absence or waive relevant expertise and review.

These are individual project responsibilities, not employer seats. Affiliation
or sponsorship does not buy roadmap priority or approval rights.
[CODEOWNERS](.github/CODEOWNERS) routes code review; it is not the scope-approval
roster. GitHub access is managed separately under repository and organization
controls. This document does not itself grant access.

## Decisions and accountability

Routine work within the project's direction may be approved by the
responsible maintainer, with bounded scope, acceptance criteria, and a
review contact recorded on the issue. The lead is not an extra approval
gate for every contribution.

New product directions, significant compatibility changes, and governance
changes need discussion among the affected maintainers. Seek agreement;
if a direction disagreement remains unresolved, the lead records a decision
and rationale. Contributors may request reconsideration with new evidence.
A disagreement with an area decision can be raised with a core maintainer.

Keep proposals and decisions public on issues or PRs. Summarize decisions
made in private meetings on the relevant issue, except confidential security,
conduct, or organizational matters. Follow [SECURITY.md](SECURITY.md) and the
[Code of Conduct](CODE_OF_CONDUCT.md) for those reports.

Human scope approval, code-review approval, and release targeting are
separate decisions. Automated recommendations, labels alone, and silence
do not authorize implementation or commit a release. An accepted issue
does not need a release milestone. The same contribution rules apply to
maintainers and automated submissions.

## Contributor progression

| Role | How to contribute | Additional responsibility |
| --- | --- | --- |
| Contributor | Report or reproduce problems; improve docs or code; review and help others. | No appointment is needed to participate. |
| Triager / reviewer | Consistently classify issues, give useful reviews, and help contributors navigate the process. | A trusted triager may manage issue metadata under the agreed policy, without merge access or automatic scope-approval authority. |
| Maintainer | Demonstrate sound judgment, collaboration, follow-through, and stewardship of an area or the whole project. | Approve scope within the recorded remit, support review, and maintain accepted changes. Merge authority depends on separately granted access. |

These are paths to responsibility, not a compulsory sequence or a contest
for the most commits. Documentation, review, triage, and contributor support
count alongside code. A contributor can remain in any role without pressure
to take on more work.

To seek a role, open an issue or ask a maintainer to sponsor a nomination.
Describe the proposed scope, relevant contributions, and realistic
availability. An existing maintainer sponsors and mentors the transition;
the core maintainers discuss it and the lead records the appointment and
rationale. Obtain the nominee's agreement and update this roster for
maintainer appointments. Access changes follow organization policy separately.

## Capacity, continuity, and stepping down

Maintainers should only invite implementation when a reviewing maintainer
can support it. Defer openly when capacity is missing rather than leaving
contributors with an indefinite promise. Review assistance and onboarding
new maintainers are part of maintenance, not work reserved for the lead.

Maintainers may reduce their scope or step down by notifying the others.
Arrange a handoff for active issues and update the roster. Review inactive
access with the individual where possible and remove permissions no longer
needed through the organization's process; stepping down is not a punishment.

Before a planned lead absence, agree who will coordinate roadmap and release
decisions and record that delegation. Routine decisions remain with the
responsible maintainers. If the lead cannot arrange a handoff, the remaining
core maintainer or maintainers choose an interim coordinator; a permanent
successor is discussed with the maintainer group and recorded publicly.

Release and infrastructure continuity also require verified backup access
under Microsoft organization controls. Naming a coordinator does not prove
that access exists; verify it before relying on the handoff.

## Changing this policy

Propose governance changes in an issue before a PR. Describe the problem,
the affected responsibilities, and the transition for existing contributors.
Record the human decision publicly and update this file and the contribution
guide together where necessary. Agent personas may advise; they do not hold
project offices or ratify governance decisions.
