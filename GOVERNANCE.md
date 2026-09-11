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

## Roadmap and release planning

Use one public native GitHub Project, with actual issues as its records.
Keep scope, evidence, and human approval on the issue; Horizon and priority
order on the Project; release targeting in the issue's milestone. Link PRs
to their issues rather than adding duplicate PR rows or draft cards. Larger
outcomes use parent issues with bounded implementation sub-issues; a parent's
priority does not approve every child.

Anyone may propose an outcome in an issue with the problem, evidence, and
alternatives. Area maintainers assess scope, risk, and maintenance cost;
core maintainers shape the shortlist, and the lead coordinates final
priority order. Use the [existing responsibilities](#maintainers-and-scope),
not a separate planning committee.

| Horizon | Meaning |
| --- | --- |
| Now | Active, reviewer-backed priorities. Start with at most three strategic outcomes alongside necessary maintenance; this is a focus guideline, not an automated cap. |
| Next | A ranked shortlist, not permission to implement. |
| Later | Deferred consideration, not a delivery promise. |

Leave Horizon empty for unselected intake. Roadmap placement, ordering, and
reactions neither authorize implementation nor guarantee delivery. Moving
or removing an issue from a Horizon does not revoke its human approval.
Record any change to approved scope or review support explicitly on the
issue. Accepted, unclaimed work still needs no second permission request:
follow [the contribution process](CONTRIBUTING.md#before-you-start-implementation),
regardless of Horizon.

Aim for a weekly asynchronous triage pass to review incoming evidence,
decisions, and contributor availability, and a monthly roadmap refresh to
reconsider the shortlist and order against capacity. These are operating
cadences, not response SLAs or mandatory meetings. Handle urgent changes
between reviews with an explicit issue decision and update the Project;
do not wait for the calendar.

### Release cohorts

Milestones hold plausible release cohorts, not every accepted proposal.
Acceptance alone implies neither a milestone nor a release date. At each
release, explicitly reconsider unfinished issues: retain the target, move
to another plausible cohort, or remove the target with a reason on the issue.
Never silently roll all unfinished work forward. Retain completed issues
and their milestone history, including completed work in the active release.

### Native Project settings

**Rollout pending:** these are intended settings, not a live board announcement.
The retired board is not the active roadmap. Creating or reopening a Project,
enabling automation, populating it, and changing live metadata require the
separate final rollout approval.

In Project settings, use public visibility and one single-select field,
`Horizon`, with `Now`, `Next`, and `Later` in that order. Leave it unset by
default. Display the native Assignees, Labels, and Milestone fields; do not
add a duplicate target-release or scope-approval field.

Save these views with `repo:microsoft/apm is:issue` plus the filters below:

| View | Layout and additional filter |
| --- | --- |
| Intake | Table: `is:open`. All open issues are visible, not implicitly approved. |
| Roadmap | Board: `is:open horizon:Now,Next,Later`. Group by Horizon in Now / Next / Later order and manually rank issues within each group, highest first. Unselected intake stays out. |
| Ready to contribute | Table: `is:open label:"status/accepted" label:"help wanted" no:assignee`. A discovery shortlist, not an approval check. |
| Release | Table: `has:milestone`, grouped by Milestone. Narrow to an existing milestone when needed; do not filter out closed issues. |

For Ready to contribute, maintainers keep labels and assignments current;
contributors check the human approval, review contact, comments, and linked
PRs for an existing claim. An unassigned issue can still be claimed. Neither
a label nor this view substitutes for the issue record.

Under **Workflows > Auto-add to project**, select `microsoft/apm` and use
`is:issue is:open`. This adds visibility through Intake, without setting
Horizon, approval, invitations, or milestones. Native auto-add handles
matching issues as they are created or updated; enabling it does not backfill
existing issues. Keep automatic archival off so closing an issue does not
silently remove release history. See GitHub's
[auto-add](https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/adding-items-automatically)
and [view-filter](https://docs.github.com/en/issues/planning-and-tracking-with-projects/customizing-views-in-your-project/filtering-projects)
instructions.

### Final rollout checklist

After explicit rollout approval, maintainers:

- [ ] Choose whether to reopen the retired Project or create its replacement;
  establish one public planning surface and verify access under existing controls.
- [ ] Configure the field and saved views above. Review built-in workflows:
  leave auto-archive off and ensure none assigns Horizon or implies approval.
- [ ] Enable the repository-scoped auto-add workflow and bulk-add existing
  open issues through the native add-items dialog. Leave Horizon empty unless
  a human has selected it; reconcile legacy metadata against issue decisions,
  preserving completed issue and release history. If replacing the board,
  keep its history accessible and include completed issues from the active
  release cohort in the new Project.
- [ ] Record human priority choices and release targets, then populate the
  selected Horizons and rank them. Confirm unselected intake stays off Roadmap,
  Ready to contribute reflects supported unclaimed work, and Release includes
  completed work as well as unfinished issues.
- [ ] Publish the verified Project link here and in the repository's Projects
  entry point, identify it as the active surface, and announce the cadence.

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
