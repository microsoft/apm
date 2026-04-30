<!--
Canonical single-comment template for the APM Review Panel skill.

Loaded ONLY at synthesis time, AFTER:
- every panelist task has returned JSON matching panelist-return-schema.json
- the apm-ceo synthesizer task has returned JSON matching ceo-return-schema.json
- the orchestrator has computed the binary verdict deterministically:
    APPROVE iff sum(len(p.required) for p in panelists if p.active) == 0
    REJECT  otherwise

The orchestrator copies this skeleton verbatim, fills the placeholders
from the collected JSON, and emits the result as exactly ONE comment via
the workflow's `safe-outputs.add-comment` channel.

Rules when filling the template:
- ASCII only. No emojis, no Unicode dashes, no box-drawing characters.
- Top-loaded order is non-negotiable: verdict, required, nits, CEO
  arbitration FIRST. Per-persona detail goes in the collapsed details
  block at the BOTTOM. Do not flip the order; the user-research is that
  PR authors must see the decision and required actions in one screen.
- Do NOT add or remove top-level sections. Adapt their bodies to the PR.
- Do NOT split this output across multiple comments under any condition.
- The Required and Nits sections are AGGREGATES across all active
  panelists. Render `[<persona>]` prefix on each item so authors know
  who raised it.
- The Per-persona detail block is the FULL JSON-derived findings list
  for each panelist - even those with required==[] and nits==[]
  (render those as "No findings.").
- Auth Expert is the only persona that can render as "Inactive --
  <reason>" in the per-persona block. Never omit its heading.
- The python-architect persona MUST contribute the two mermaid diagrams
  and the Design patterns subsection in its `extras.diagrams` payload;
  surface them inside its per-persona block.
-->

## APM Review Panel Verdict: <APPROVE|REJECT>

> <one-line CEO arbitration headline; e.g. "All five specialists agreed; no required changes." or "Architecture is sound but supply-chain finding blocks merge.">

### Required before merge (<N> items)

<If N == 0:>
None.
<else, render aggregated required[] across all panelists:>
- [<persona-slug>] <finding.summary> <if file/line: at `<file>:<line>`>
  - Why: <finding.rationale>
  - <if suggestion: Suggested fix: <finding.suggestion>>
- ...

### Nits (<M> items, skip if you want)

<If M == 0:>
None.
<else, render aggregated nits[]:>
- [<persona-slug>] <finding.summary> <if file/line: at `<file>:<line>`>
- ...

### CEO arbitration

<ceo.arbitration prose, one to three paragraphs.>

<if ceo.dissent_notes is non-empty:>
**Dissent resolved:** <ceo.dissent_notes>

<if ceo.growth_signal is non-empty:>
**Growth/positioning note:** <ceo.growth_signal>

---

<details>
<summary>Per-persona findings (full)</summary>

#### Python Architect

<if active:>
<Include the OO/class mermaid diagram and execution-flow mermaid
diagram from extras.diagrams. Then render required[] + nits[] in full;
if both empty, write "No findings.">
<else: should never be inactive>

#### CLI Logging Expert

<Render required[] + nits[] in full; "No findings." if both empty.>

#### DevX UX Expert

<Render required[] + nits[] in full; "No findings." if both empty.>

#### Supply Chain Security Expert

<Render required[] + nits[] in full; "No findings." if both empty.>

#### Auth Expert

<if active:>
<Render required[] + nits[] in full; "No findings." if both empty.>
<else:>
Inactive -- <inactive_reason>

#### OSS Growth Hacker

<Render required[] + nits[] in full; "No findings." if both empty.
Surface any side-channel growth/positioning note inline; CEO already
echoed it in the headline section if relevant.>

</details>

<sub>Verdict computed deterministically: <N> required findings across
<K> active panelists. APPROVE iff N == 0. Push a new commit to clear
this verdict label automatically.</sub>
