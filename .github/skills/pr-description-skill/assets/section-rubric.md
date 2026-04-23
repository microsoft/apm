<!--
  section-rubric.md

  Loaded by pr-description-skill at the self-check step. For each
  section, run the acceptance test, fix or rewrite if it fails,
  re-run until it passes.

  CHARSET: this file is ASCII (lives in the repo, subject to
  .github/instructions/encoding.instructions.md). The PR body it
  governs is UTF-8 GitHub-Flavored Markdown.
-->

# Section Rubric -- Per-section Quality Bar and Self-check

## Body-level ceilings

- Total body length: target 150-220 lines. 250+ triggers a
  tightening pass; 300+ is refused.
- Long evidence (full audit logs, full pytest transcripts, large
  file listings) MUST be wrapped in `<details>` so the visible body
  stays scannable.

## 1. Title line

- Acceptance: First line is `<verb>(<scope>): <summary>`. Verb is
  one of {add, fix, refactor, harden, document, ship, remove,
  deprecate}. Max 100 chars.
- Refuse: past tense ("added"), single-file scope ("update SKILL.md"
  instead of an area).

## 2. TL;DR

- Acceptance: 2-4 sentences. States what changed, why now, risk
  eliminated.
- Refuse: more than 4 sentences; marketing adjectives in {great,
  amazing, significantly, best-in-class, powerful}; restating the
  title without adding the "why now".

## 3. Problem (WHY)

- Acceptance: max 6 bullets. Each tagged `[x]` or `[!]`. Max 3
  verbatim quoted anchors total in this section. Each quote
  reproduced character-for-character at its linked URL.
- Refuse: hypothetical-only language ("could lead to") with no
  observed evidence; paraphrased quotes inside link text; anchors
  to anything other than PROSE / Agent Skills (or a canonical ref
  the orchestrator was asked to use); more than 3 quotes (they stop
  adding signal beyond that).

## 4. Approach (WHAT)

- Acceptance: a table with columns `#`, `Fix`, `Principle`,
  `Source`, OR a 3-7 bullet list. Every Principle cell is a
  verbatim quote with a hyperlink. May be replaced by the single
  line "Additive change -- see Implementation" when the PR adds new
  surface without changing existing behavior.
- Refuse: empty Principle/Source cells; paraphrased principles
  ("basically Progressive Disclosure"); one mega-row covering
  everything in the PR.

## 5. Implementation (HOW)

- Acceptance: one short paragraph per file (or a table). Each entry
  names intent and surgical-scope notes. May reference the diff via
  a permalink (`https://github.com/microsoft/apm/blob/<sha>/...#Ln-Lm`)
  rather than restating it.
- Refuse: line-by-line restatement of the diff; "refactored for
  clarity" with no specific intent; files in the activation
  contract that have no entry here at all.

## 6. Diagrams

- Acceptance: 1-3 mermaid blocks for any non-doc-only PR. Each
  block preceded by a one-sentence legend. **Every block validated
  by `mmdc` before save** (see SKILL.md "Mandatory mermaid
  validation step").
- Refuse: any block that fails `mmdc`; a third diagram that does
  not earn its place; diagrams with no legend; decorative diagrams
  that do not reflect the change. Unicode IS allowed in node
  labels -- the constraint is mmdc validity, not ASCII purity.

## 7. Trade-offs

- Acceptance: 3-5 bullets for cross-cutting changes; 1-2 acceptable
  for mechanical PRs. Each bullet names option chosen, option
  rejected, rationale.
- Refuse: trade-offs that read like benefits in disguise; "no
  trade-offs" claim on a non-mechanical PR; rationale that boils
  down to "personal preference" with no grounding.

## 8. Benefits

- Acceptance: 3-5 numbered items. Each names something a reviewer
  can verify (count, presence, behavior under specific input).
- Refuse: marketing adjectives; benefits that restate the fix
  without naming the observable outcome.

## 9. Validation

- Acceptance: real CLI output, verbatim. Commands named on the
  line immediately preceding their fenced block. Long transcripts
  wrapped in `<details>`.
- Refuse: invented or stylized output; output excerpts that hide
  failures with `...`; narration of what the command "would print"
  in place of the actual output.

## 10. How to test

- Acceptance: max 5 numbered or task-list steps. Each step has an
  action and an expected observation. Use GFM task list form
  (`- [ ] ...`) so reviewers can tick boxes as they go.
- Refuse: "see the diff" or "obvious from the code" as a step;
  steps that depend on un-mentioned setup; steps that rely on a
  private fixture.

## GFM features the body SHOULD use

- `> [!NOTE]`, `> [!TIP]`, `> [!IMPORTANT]`, `> [!WARNING]`,
  `> [!CAUTION]` for callouts that need to interrupt scanning.
- `<details><summary>...</summary>...</details>` for long evidence.
- Task lists in How to test.
- Tables with `:---:` / `---:` alignment for matrices.
- Permalinks to the diff for line-level evidence.

If the draft contains no alerts, no collapsibles, and no task
lists, ask whether GFM features would help -- a flat 250-line body
almost always benefits from at least one collapsible around its
validation block.

## Final pass -- run before saving

- [ ] All 10 sections present.
- [ ] Total body length within 150-220 lines (250+ triggers
      tightening).
- [ ] No `<PLACEHOLDER>`, `TBD`, or `TODO` remains.
- [ ] Every quote appears verbatim at its linked URL (spot-check
      at least 3).
- [ ] **Every mermaid block validated by `mmdc`.**
- [ ] TL;DR sentence count is 4 or fewer.
- [ ] Long evidence is inside `<details>`, not flat in the body.
- [ ] Trailer line `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>` is the last non-empty line.
