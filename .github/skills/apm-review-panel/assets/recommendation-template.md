<!--
APM Review Panel - recommendation comment template (advisory regime).

DESIGN PRINCIPLE: this comment is for a busy OSS maintainer triaging a
PR queue. They have ~30 seconds. The TOP must answer:
  1. What does the panel think (stance)?
  2. Why (one paragraph)?
  3. What are the 1-3 highest-signal items?
Everything else is collapsed or omitted unless it adds signal.

RENDERING RULES (the orchestrator follows these literally):

- ASCII only. No emojis, no Unicode dashes, no box-drawing characters.
- The panel is ADVISORY. NEVER render the words "Verdict", "APPROVE",
  "REJECT", "blocked", "merge gate", or any equivalent.
- Sections are SKIPPED (not rendered as empty placeholders) when their
  source field is empty or missing. A section that adds no signal is
  worse than no section.
- The per-persona table renders ONLY active panelists.
- The recommended follow-ups list is CAPPED at 5 items; the CEO is
  asked to curate. If the CEO returns more, take the first 5.
- Architecture diagrams render ONLY when python_architect.extras.diagrams
  supplies them. NEVER invent diagrams; NEVER render a placeholder.
- Principle alignment renders ONLY when ceo.principle_alignment has at
  least one non-empty value, and inline as a single short paragraph
  (not a bullet list) to keep the comment compact.
- Growth amplification renders ONLY when non-empty AND the PR is
  non-trivial (CEO judgment encoded in arbitration prose; if growth
  field is non-empty, render it).
- The full per-persona findings live in a <details> block at the
  bottom. Out of sight unless the maintainer wants depth.
-->

## APM Review Panel: `{{ ceo.ship_recommendation.stance }}`

> {{ ceo.headline }}

{{ ceo.arbitration }}

{{#if ceo.dissent_notes }}
**Dissent.** {{ ceo.dissent_notes }}
{{/if}}

{{#if has_any_principle_alignment }}
**Aligned with:** {{ ceo.principle_alignment | inline_humanize_join }}
{{/if}}

{{#if ceo.growth_amplification }}
**Growth signal.** {{ ceo.growth_amplification }}
{{/if}}

### Panel summary

| Persona | B | R | N | Takeaway |
|---|---|---|---|---|
{{#each active_panelists }}
| {{ persona | humanize }} | {{ count_blocking }} | {{ count_recommended }} | {{ count_nits }} | {{ summary }} |
{{/each}}

> B = blocking-severity findings, R = recommended, N = nits.
> Counts are signal strength, not gates. The maintainer ships.

{{#if ceo.recommended_followups.length }}
### Top {{ min(5, ceo.recommended_followups.length) }} follow-ups

{{#each ceo.recommended_followups[:5] }}
{{ @index_plus_1 }}. **[{{ from_persona | humanize }}]{{#if blocking }} *(blocking-severity)*{{/if}}** {{ summary }} -- {{ why }}
{{/each}}
{{/if}}

{{#if python_architect.extras.diagrams.component }}
### Architecture

```mermaid
{{ python_architect.extras.diagrams.component }}
```
{{/if}}

{{#if python_architect.extras.diagrams.sequence }}
```mermaid
{{ python_architect.extras.diagrams.sequence }}
```
{{/if}}

### Recommendation

{{ ceo.ship_recommendation.prose }}

---

<details>
<summary>Full per-persona findings</summary>

{{#each panelists_in_canonical_order }}
#### {{ persona | humanize }}{{#unless active }} -- inactive{{/unless}}

{{#if active }}
{{#if findings.length }}
{{#each findings }}
- **[{{ severity }}]** {{ summary }}{{#if file }} at `{{ file }}{{#if line }}:{{ line }}{{/if}}`{{/if}}
  {{ rationale }}
  {{#if suggestion }}
  *Suggested:* {{ suggestion }}
  {{/if}}
{{/each}}
{{else}}
No findings.
{{/if}}
{{else}}
{{ inactive_reason }}
{{/if}}

{{/each}}
</details>

<sub>This panel is advisory. It does not block merge. Re-apply the
`panel-review` label after addressing feedback to re-run.</sub>
