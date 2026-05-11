## Docs sync advisory

Verdict: **{{ verdict }}**  *  Pages affected: {{ pages_affected_count }}  *  LLM calls: {{ llm_calls_used }}/15  *  Took: {{ elapsed_seconds }}s

{{ #if cost_ceiling_hit }}
> WARNING: Hit the 15 LLM call ceiling. Result is partial; see `cost_ceiling_hit: true` flag.
{{ /if }}

{{ #if cdo_disagreement_noted }}
> NOTE: CDO disagreement after 3 redraft rounds. Maintainer judgement needed; see "Open concerns" below.
{{ /if }}

### Summary

{{ summary_paragraph }}

{{ #if pages_affected_count == 0 }}

No documentation changes needed for this PR.

{{ classifier_reasoning }}

{{ else }}

### Proposed patches

{{ #each page_patches }}

#### `{{ this.page }}`  ({{ this.persona }} ramp, promise {{ this.promise }})

{{ #each this.sections }}

**Section: {{ this.section }}**  (lines {{ this.line_range }})

```diff
- {{ this.before }}
+ {{ this.after }}
```

Rationale: {{ this.rationale }}

{{ #if this.verifications }}
Verified by: {{ this.verifications }}
{{ /if }}

{{ /each }}

{{ /each }}

{{ /if }}

{{ #if structural_proposal }}

### Structural proposal

{{ structural_proposal.summary }}

**New pages:**

{{ #each structural_proposal.new_pages }}
- `{{ this.slug }}` -- {{ this.title }} ({{ this.persona }} ramp). {{ this.rationale }}
{{ /each }}

**Moved / retired:**

{{ #each structural_proposal.moved_pages }}
- `{{ this.from }}` -> `{{ this.to }}` ({{ this.redirect_rationale }})
{{ /each }}

{{ #if structural_proposal.confirm_label_present }}

A companion docs PR has been opened: {{ companion_pr_link }}.

{{ else }}

To open a companion docs PR with these changes, apply the `docs-sync-confirm` label to this PR.

{{ /if }}

{{ /if }}

{{ #if open_concerns }}

### Open concerns (from CDO)

{{ #each open_concerns }}
- {{ this }}
{{ /each }}

{{ /if }}

---

<details>
<summary>How this advisory was produced</summary>

- Classifier verdict: `{{ verdict }}` (confidence: {{ confidence }}, source: {{ classifier_source }})
- Panel composition: {{ panel_composition }}
- Tool-verified claims: {{ verification_count }} ({{ verification_pass_count }} verified, {{ verification_refute_count }} refuted, {{ verification_inconclusive_count }} inconclusive)
- CDO redraft rounds: {{ cdo_redraft_rounds }}/3

This is an advisory comment from the `docs-sync` skill ([source](.apm/skills/docs-sync/SKILL.md)). It does not gate merge. The maintainer ships.

Re-run by removing and re-applying the `docs-sync` label.

</details>
