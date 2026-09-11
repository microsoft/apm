<!--
Load ONLY at synthesis time. Fill one advisory comment, under 250 lines,
ASCII only, retaining all headings and six persona sections. Inactive
conditional lenses say "Not activated -- <reason>". The caller is the
sole writer; direct invocation returns this without posting.
The v2 JSON tail is advisory data, not a label/milestone write instruction.
-->

<!-- apm-triage-advisory:v2 -->
## Triage recommendation

<accept | needs-design | decline-with-reason | duplicate-of | defer-later | auto-handle>

**Recommendation only, not scope approval.** A responsible human maintainer
must approve the scope and review contact before implementation. Labels,
automated advice, and silence are not approval or a release commitment.

## Proposed classification

<Only useful type/area/theme labels from the label contract, or none.
Existing human choices stay unchanged; name conflicts rather than replacing them.>

## Proposed scope brief

- **Scope:** <bounded deliverable or the missing information>
- **Done when:** <observable completion criteria>
- **Exclusions:** <important non-goals, or none identified>
- **Review needs:** <expertise and capacity still to confirm; no invented assignment>

## Suggested next action

<One concrete discussion/reproduction/design/human-review step. Do not invite
implementation based on the recommendation alone.>

## Suggested issue comment

```markdown
<Warm, specific, evidence-grounded reply. Explain the recommendation and
uncertainty. Link relevant documentation when useful. Do not imply that the
project has accepted, assigned, or release-targeted this work.>
```

## Per-lens notes (collapsed)

<details>
<summary>DevX UX Expert -- User-Need Reviewer</summary>

<User task, capability fit, and supporting evidence.>

</details>

<details>
<summary>Supply Chain Security Expert -- Risk-Surface Reviewer</summary>

<Risk surfaces and required review expertise, or no risk-surface implication.>

</details>

<details>
<summary>OSS Growth Hacker -- Contributor-Tone Reviewer</summary>

<Tone guidance, or Not activated -- reason.>

</details>

<details>
<summary>Python Architect -- Architecture Reviewer</summary>

<Feasibility and unresolved design boundaries, or Not activated -- reason.>

</details>

<details>
<summary>Doc Writer -- Documentation Reviewer</summary>

<Direct documentation impact, or Not activated -- reason.>

</details>

<details>
<summary>APM CEO -- Triage Arbiter</summary>

<Synthesize advice, resolve lens disagreements, and name uncertainty.
This persona cannot ratify scope or speak for a human maintainer.>

</details>

```json triage-recommendation
{
  "schema_version": 2,
  "advisory_only": true,
  "recommendation": "<accept | needs-design | decline-with-reason | duplicate-of | defer-later | auto-handle>",
  "recommendation_detail": "<reason or verified duplicate reference>",
  "classification": {
    "theme": null,
    "areas": [],
    "type": null
  },
  "proposed_brief": {
    "scope": "<bounded change>",
    "done_when": "<observable completion criteria>",
    "exclusions": "<non-goals>",
    "review_needs": "<expertise and capacity to confirm>"
  },
  "next_action": "<one sentence>",
  "comment_markdown": "<same suggested issue comment as above>"
}
```
