<!--
Canonical single-comment template for the APM Review Panel skill.

Load this file ONLY at synthesis time, after every panelist has produced
its findings. The orchestrator copies this skeleton verbatim, fills the
placeholders, and emits the result as exactly ONE comment via the
workflow's `safe-outputs.add-comment` channel.

Rules when filling the template:
- ASCII only. No emojis, no Unicode dashes, no box-drawing characters.
- Keep total length under ~600 lines.
- Do NOT add or remove top-level sections. Adapt their bodies to the PR.
- Do NOT split this output across multiple comments under any condition.
- Routing changes which personas run, not which persona headings appear.
- Only Auth Expert is conditional. If it was not activated for the PR,
  write "Not activated -- <reason>" as that persona's body. Do not omit
  the persona heading. All other persona headings always have findings.
- The Python Architect block MUST contain the two mermaid diagrams and
  the Design patterns subsection from the python-architect persona's
  PR review output contract. If those are missing, the synthesis is
  incomplete -- re-invoke the Python Architect before emitting.
-->

## APM Review Panel Verdict

**Disposition**: <APPROVE | REQUEST_CHANGES | NEEDS_DISCUSSION> <optional one-line qualifier, e.g. "(with two minor pre-merge fixes)">

---

### Per-persona findings

**Python Architect**: <findings; MUST include the OO/class mermaid diagram, the execution-flow mermaid diagram, and the Design patterns subsection per the python-architect persona's PR review output contract>

**CLI Logging Expert**: <findings>

**DevX UX Expert**: <findings>

**Supply Chain Security Expert**: <findings>

**Auth Expert**: <findings, or "Not activated -- <reason citing the touched files>">

**OSS Growth Hacker**: <findings; if relevant, include side-channel note to CEO about conversion / growth-strategy implications>

---

### CEO arbitration

<one-paragraph synthesis from apm-ceo: resolve any disagreements between specialists, ratify the disposition, and state the strategic call. If specialists agreed and the change is uncontroversial, say so plainly in one or two sentences.>

---

### Required actions before merge

1. <required action with concrete pointer (file path, line, diff suggestion). If Disposition is APPROVE with no required actions, write "None." here -- do not omit the section.>
2. <...>

---

### Optional follow-ups

- <follow-up suggestion that is out of scope for this PR but worth tracking>
- <...>
