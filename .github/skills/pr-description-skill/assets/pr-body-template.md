<!--
  pr-body-template.md

  Loaded by pr-description-skill ONLY at synthesis time, after every
  row of the activation contract has been filled. Replace every
  <PLACEHOLDER> with content drawn from the activation contract
  inputs. Drop a section's body (keep its header) only when SKILL.md
  explicitly allows it for this PR shape.

  CHARSET: this template is ASCII because it lives in the repo and is
  subject to .github/instructions/encoding.instructions.md. The
  rendered PR body the orchestrator produces from this template MUST
  be UTF-8 GitHub-Flavored Markdown -- use em dashes, smart quotes,
  alerts, collapsibles, and Unicode where they improve readability.
-->

# <verb>(<scope>): <one-line imperative summary>

## TL;DR

<2-4 sentences: what changed, why now, the risk this eliminates.>

> [!NOTE]
> <Optional one-line callout: linked issue, follow-up plan, or the
> single fact a reviewer most needs to know up front.>

## Problem (WHY)

<Up to 6 bullets. Tag each with [x] hard violation or [!] soft risk.
Up to 3 verbatim quoted anchors total across this section.>

- [x] <Concrete failure mode 1, with file or command evidence.>
- [x] <Concrete failure mode 2.>
- [!] <Soft risk or drift vector observed today.>

Why these matter: <one or two sentences naming the principle, rule,
or contract each failure breaks. Anchor each claim to the most
credible source available for THIS PR -- could be a doc URL, a file
path with line range, a prior PR or issue, a verbatim CLI/log
excerpt, or a named convention. Omit the anchor entirely when no
credible source exists; never invent one. Bullet style, link style,
or inline-quote style are all acceptable -- match what the source
naturally affords.>

## Approach (WHAT)

<Table OR 3-7 bullets. If purely additive, replace this section's
body with: "Additive change -- see Implementation.">

<Use a 2-column table when each fix has one obvious anchor; use 3
columns only when the "why" cannot be merged into the fix line
without loss. Anchor type is open: a URL, a principle name, a file
ref, a prior PR/issue, or no anchor at all. Drop the anchor column
entirely if most rows would have none.>

| # | Fix (and why, if non-obvious) |
|---|-------------------------------|
| 1 | <One-line surgical fix; trailing parenthetical or footnote anchor only when it adds reviewer signal.> |
| 2 | <Surgical fix.> |
| 3 | <Surgical fix.> |

## Implementation (HOW)

<One short paragraph per file changed, OR a table. No prose walls.
Permalink to the diff for line-level evidence:
https://github.com/microsoft/apm/blob/<sha>/path#L12-L34>

- **`<path/to/file/1>`** -- <intent in one or two sentences;
  what was deliberately not touched and why.>
- **`<path/to/file/2>`** -- <intent; anchor if non-mechanical:
  per ["<quote>"](<url>).>

## Diagrams

<1-3 mermaid blocks. Each preceded by a one-sentence legend. Every
block MUST have been validated by mmdc before saving.>

<DO NOT paste an example diagram here. Derive each diagram from THIS
PR's actual artifacts -- nodes are real file names, function names,
job IDs, or component names from the diff; edges are real
control-flow or data-flow steps; branch labels are the real
predicates being decided; side effects (writes, network calls,
process exits) are visibly marked. A flowchart with placeholder
labels like "Start", "Decision", "Action when yes" is a failed
diagram and must be rewritten or removed.

Pick the diagram type that matches the change: flowchart for
execution flow, sequenceDiagram for cross-component interaction,
stateDiagram-v2 for lifecycle, classDiagram for type relationships,
erDiagram for data shape. ASCII labels only inside the block.>

Legend: <one sentence on what this diagram shows and what a
reviewer should look at first.>

<mermaid block goes here -- omit the entire Diagrams section if no
relationship in this PR is genuinely non-trivial. A trivial diagram
is worse than no diagram.>

<Add a second diagram only if the relationships are non-trivial,
and only after the first diagram passes mmdc validation.>

## Trade-offs

<3-5 bullets. 1-2 acceptable for mechanical PRs.>

- **<Decision in one phrase>.** Chose <option>; rejected <option>
  because <rationale, ideally anchored:
  ["<quote>"](<url>)>.
- **Pre-existing issue X left in place.** Surgical scope; right
  venue is a separate PR.

## Benefits

<3-5 numbered, measurable items.>

1. <Benefit a reviewer can verify, e.g. "One PR comment per review
   run instead of N".>
2. <Measurable benefit.>
3. <Measurable benefit.>

## Validation

<Real CLI output, verbatim. Long transcripts go in <details>.>

`apm audit --ci`:

```
<verbatim CLI output>
```

<details><summary>Full pytest output (N tests)</summary>

```
<verbatim transcript>
```

</details>

## How to test

<Max 5 numbered or task-list steps. Each step has an action and an
expected observation.>

- [ ] <Step 1: action -> expected observation.>
- [ ] <Step 2.>
- [ ] <Step 3.>

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
