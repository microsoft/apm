<!--
APM Spec Guardian - PR comment template (advisory regime).

DESIGN PRINCIPLE: this comment is for a maintainer triaging a spec PR.
They have ~60 seconds. The TOP must answer:
  1. What does the panel think (ship_decision)?
  2. How shocked are they (shocked_meter_avg + convergence table)?
  3. What 1-5 things should I fold before merging?
Everything else collapses under <details>.

RENDERING RULES (orchestrator follows literally):

- ASCII only (U+0020 - U+007E). No emojis, no Unicode dashes, no
  box-drawing characters, no curly quotes.
- The panel is ADVISORY. NEVER render the words "Verdict", "APPROVE",
  "REJECT", "blocked", "merge gate", or any equivalent.
- Sections are SKIPPED (not rendered as empty placeholders) when their
  source field is empty or missing.
- The convergence table renders all 4 panelists.
- The fold-now list renders the synthesizer's fold_now[] verbatim.
- The defer-v0.1.1 and defer-v0.2 lists render inside <details> if
  either has more than 3 items.
- Linter notes render only if any check failed.
- Full per-panel findings live in a <details> block at the bottom.
- The {{ wave_0_scope }} line at the top documents the orchestrator's
  Wave 0 classification ("editorial-only" / "editorial-patch" /
  "new-version") and the diff size, so the reader sees why the panel
  ran (or did not).
- The editorial-only branch (wave_0_scope == "editorial-only") renders
  ONLY the header + the linter-notes section + a one-line "no
  substantive spec change detected" sentence; everything else is
  skipped.
-->

## APM Spec Guardian: `{{ synthesizer.ship_decision }}`

> Scope: **{{ wave_0_scope }}**; diff = +{{ diff_added }}/-{{ diff_removed }} lines across {{ diff_files }} file(s). Shocked-meter avg: **{{ synthesizer.shocked_meter_avg }}/10**.

{{#if (eq wave_0_scope "editorial-only") }}
No substantive spec change detected. Wave 3 panel fan-out skipped; only the linter ran on the modified artifact.

{{#if linter_failures.length }}
### Linter notes ({{ linter_failures.length }} check(s) failed)

{{#each linter_failures }}
- **[{{ id }}]** {{ summary }}
{{/each}}
{{else}}
Linter: all 11 checks PASS.
{{/if}}

<sub>This panel is advisory. It does not block merge. Re-apply the `spec-review` label to re-run.</sub>
{{else}}

{{ synthesizer.ship_prose }}

### Convergence

| Panel | Verdict | Shocked | New B | New R | New N |
|---|---|---:|---:|---:|---:|
{{#each synthesizer.convergence_table }}
| {{ panel | humanize }} | {{ verdict }} | {{ shocked_meter }}/10 | {{ new_blockers }} | {{ new_recommended }} | {{ new_nits }} |
{{/each}}

> B = new blocking findings, R = new recommended, N = new nits.
> Counts are signal strength, not gates. The maintainer ships.

{{#if synthesizer.convergent_themes.length }}
### Convergent themes (flagged by 2+ panels)

{{#each synthesizer.convergent_themes }}
- **{{ id }} -- {{ label }}** (supporting: {{ supporting_finding_ids | comma_join }})
{{/each}}
{{/if}}

{{#if synthesizer.fold_now.length }}
### Fold now ({{ synthesizer.fold_now.length }} item(s))

{{#each synthesizer.fold_now }}
{{ @index_plus_1 }}. **[{{ id }} / {{ theme }}] {{ spec_section }}** -- {{ patch_instruction }}
   *Success criterion:* `{{ success_criterion }}`
{{/each}}
{{/if}}

{{#if synthesizer.defer_v0_1_1.length }}
{{#if (gt synthesizer.defer_v0_1_1.length 3) }}
<details>
<summary>Defer to v0.1.1 ({{ synthesizer.defer_v0_1_1.length }} items)</summary>

{{#each synthesizer.defer_v0_1_1 }}
- **[{{ id }} / {{ theme }}] {{ spec_section }}** -- {{ patch_instruction }}
{{/each}}
</details>
{{else}}
### Defer to v0.1.1

{{#each synthesizer.defer_v0_1_1 }}
- **[{{ id }} / {{ theme }}] {{ spec_section }}** -- {{ patch_instruction }}
{{/each}}
{{/if}}
{{/if}}

{{#if synthesizer.defer_v0_2.length }}
{{#if (gt synthesizer.defer_v0_2.length 3) }}
<details>
<summary>Defer to v0.2 ({{ synthesizer.defer_v0_2.length }} items)</summary>

{{#each synthesizer.defer_v0_2 }}
- **[{{ id }} / {{ theme }}] {{ spec_section }}** -- {{ patch_instruction }}
  *Reserved slot:* `{{ reserved_slot_anchor }}`
{{/each}}
</details>
{{else}}
### Defer to v0.2

{{#each synthesizer.defer_v0_2 }}
- **[{{ id }} / {{ theme }}] {{ spec_section }}** -- {{ patch_instruction }}
  *Reserved slot:* `{{ reserved_slot_anchor }}`
{{/each}}
{{/if}}
{{/if}}

{{#if synthesizer.reject.length }}
### Rejected findings

{{#each synthesizer.reject }}
- **{{ finding_id }}** -- {{ rationale }}
{{/each}}
{{/if}}

{{#if linter_failures.length }}
### Linter notes ({{ linter_failures.length }} check(s) failed)

{{#each linter_failures }}
- **[{{ id }}]** {{ summary }}
{{/each}}

{{#if (eq synthesizer.ship_decision "fold_and_ship") }}
> Note: the synthesizer recommends ship, but the linter found issues worth folding first. Address these in the same PR if cheap.
{{/if}}
{{/if}}

{{#if synthesizer.linter_handoff_notes }}
> **Linter handoff:** {{ synthesizer.linter_handoff_notes }}
{{/if}}

---

<details>
<summary>Full per-panel findings</summary>

{{#each panelists_in_canonical_order }}
#### {{ persona | humanize }} -- shocked_meter {{ shocked_meter }}/10, confidence {{ confidence }}

*Summary:* {{ summary }}

{{#if new_blocking_findings.length }}
**New blocking findings ({{ new_blocking_findings.length }})**
{{#each new_blocking_findings }}
- **[{{ id }}] {{ section_ref }}** -- {{ finding }}
  *Recommended fix:* {{ recommended_fix }}
{{/each}}
{{/if}}

{{#if new_recommended_findings.length }}
**New recommended findings ({{ new_recommended_findings.length }})**
{{#each new_recommended_findings }}
- **[{{ id }}] {{ section_ref }}** -- {{ finding }}
  *Recommended fix:* {{ recommended_fix }}
{{/each}}
{{/if}}

{{#if new_nit_findings.length }}
**New nit findings ({{ new_nit_findings.length }})**
{{#each new_nit_findings }}
- **[{{ id }}]** {{ finding }}{{#if recommended_fix }} -- *fix:* {{ recommended_fix }}{{/if}}
{{/each}}
{{/if}}

{{#if regressions_in_praised_strengths.length }}
**Regressions in praised strengths**
{{#each regressions_in_praised_strengths }}
- {{ this }}
{{/each}}
{{/if}}

{{#if preserved_strengths_confirmed.length }}
**Preserved strengths confirmed**
{{#each preserved_strengths_confirmed }}
- {{ this }}
{{/each}}
{{/if}}

{{/each}}
</details>

<sub>This panel is advisory. It does not block merge. Re-apply the `spec-review` label after addressing feedback to re-run.</sub>
{{/if}}
