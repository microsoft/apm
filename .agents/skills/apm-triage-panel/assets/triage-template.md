<!--
Canonical single-comment template for the APM Triage Panel skill.

Load this file ONLY at synthesis time, after every panelist has produced
its findings and the CEO arbiter has resolved the decision. The
orchestrator copies this skeleton verbatim, fills the placeholders, and
emits the result as exactly ONE comment via the workflow's
`safe-outputs.add-comment` channel.

Rules when filling the template:
- ASCII only. No emojis, no Unicode dashes, no box-drawing characters.
- Keep total length under ~400 lines.
- Do NOT add or remove top-level sections. Adapt their bodies to the issue.
- Do NOT split this output across multiple comments under any condition.
- Routing changes which personas run, not which persona headings appear
  in the per-lens notes block.
- OSS Growth Hacker, Python Architect, and Doc Writer are conditional.
  If one was not activated for the issue, write
  "Not activated -- <reason>" as that persona's body. Do not omit any
  persona heading.
- The trailing ```json block named `triage-decision` is REQUIRED and is
  the machine-readable contract for label application, milestone setting,
  and comment posting. Keep its keys exactly as written below.
-->

## Triage decision

<one of: `accept` | `needs-design` | `decline-with-reason: <text>` | `duplicate-of: #N` | `defer-later` | `auto-handle: <action>`>

## Proposed labels

```
theme/<mega>
area/<sub>
area/<sub-optional>
type/<one>
status/<one>
priority/<one-optional>
<preserved-label-if-any>
```

## Milestone

<milestone title, e.g. `0.9.x` or `0.10.0`, or `null`>

## Suggested next action

<one sentence; concrete and actionable, e.g. "Draft a design doc for the marketplace name-resolution rule and link it back here.">

## Suggested issue comment

```markdown
<the actual reply that should be posted to the issue.
Tone: warm, specific, README-grounded, ASCII-only.
Cite the triage decision rubric reason in plain language.
Link to the relevant README section
(https://github.com/microsoft/apm#...) or the roadmap discussion
(https://github.com/microsoft/apm/discussions/116) when relevant.
End with a concrete next step the author can take.>
```

## Per-lens notes (collapsed)

<details>
<summary>DevX UX Expert -- User-Need Reviewer</summary>

<one paragraph: does the request map to a real surface? what user task does it solve? is the framing aligned with the npm/pip/cargo mental model APM commits to? if not, what would the user actually need?>

</details>

<details>
<summary>Supply Chain Security Expert -- Risk-Surface Reviewer</summary>

<one paragraph: does this touch any P/G/S surface (packaging, governance, signing, lockfile, marketplace name resolution, MCP config trust, auth)? if so, which `theme/*` is required and which `area/*` ones? if not, state "no risk-surface implication".>

</details>

<details>
<summary>OSS Growth Hacker -- Contributor-Tone Reviewer</summary>

<one paragraph: tone-tuning for the suggested comment given author signals (first-time contributor? low prior interaction count?). Or: "Not activated -- <one sentence reason>".>

</details>

<details>
<summary>Python Architect -- Architecture Reviewer</summary>

<one paragraph: feasibility of the design as proposed, cross-cutting impact across modules / files / contracts, and whether the issue should land as `status/needs-design` instead of `status/accepted`. Or: "Not activated -- <one sentence reason>".>

</details>

<details>
<summary>Doc Writer -- Documentation Reviewer</summary>

<one paragraph: is docs work implied? should `area/docs-site` ride as a secondary `area/*` so the implementing PR is reminded? is the suggested comment wording clear and grounded in the user vocabulary used in the README and guides? Or: "Not activated -- <one sentence reason>".>

</details>

<details>
<summary>APM CEO -- Triage Arbiter</summary>

<one paragraph: synthesis. Resolve any disagreements between specialists, ratify the decision, theme, milestone, priority, and reply tone. State the strategic call in one or two sentences.>

</details>

```json triage-decision
{
  "decision": "<accept | needs-design | decline-with-reason | duplicate-of | defer-later | auto-handle>",
  "decision_detail": "<free-form text for decline reason, duplicate target, or auto-handle action; empty string otherwise>",
  "theme": "<theme/portability | theme/security | theme/governance | null>",
  "areas": ["area/<sub>", "area/<sub-optional>"],
  "type": "<type/bug | type/feature | type/docs | type/refactor | type/architecture | type/automation | type/release | type/performance>",
  "status": "<status/accepted | status/needs-design | status/blocked | status/in-flight>",
  "priority": "<priority/high | priority/low | null>",
  "preserved_labels": ["<breaking-change | good first issue | help wanted | experimental | panel-review | dx | agentic-workflows | dependencies>"],
  "milestone": "<milestone-title-or-null>",
  "next_action": "<one sentence>",
  "comment_markdown": "<the same markdown body emitted in the Suggested issue comment block above, ready to post>"
}
```
