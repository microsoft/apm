---
name: pr-description-skill
description: >-
  Use this skill to write the PR description (PR body) for any pull
  request opened against microsoft/apm. Produces one self-sufficient
  GitHub-Flavored Markdown artifact: TL;DR, Problem (WHY), Approach
  (WHAT), Implementation (HOW), 1-3 validated mermaid diagrams,
  explicit trade-offs, validation evidence, and a How-to-test
  section -- with every WHY-claim backed by a verbatim quote from
  PROSE or Agent Skills. Activate when the user asks to "write a PR
  description", "draft a PR body", "open a PR", "fill in the PR
  template", or any equivalent.
---

# PR Description Skill -- Anchored, Concise, Validated PR Bodies

## When to use

Trigger this skill on any of the following intents:

- "write a PR description"
- "draft a PR body"
- "open a PR" / "open this PR" / "let's open the PR"
- "fill in the PR template"
- "summarize this branch as a PR"
- "create the PR write-up"

Reusable for any PR against `microsoft/apm`. The output is one
markdown file that the orchestrator pastes into
`gh pr create --body-file` or surfaces to the maintainer.

## Output charset rule (read this first)

The repo-wide encoding rule at
`.github/instructions/encoding.instructions.md` constrains
**source files and CLI output** to printable ASCII because Windows
cp1252 terminals raise `UnicodeEncodeError` on anything else. PR
comments are NOT source code and NOT CLI output -- they are rendered
by GitHub's Primer engine, which expects UTF-8 GitHub-Flavored
Markdown.

Two distinct rules therefore apply:

1. **Source files in this bundle** (`SKILL.md`, `assets/*`) MUST
   stay ASCII. They live in the repo and are subject to
   `.github/instructions/encoding.instructions.md`.
2. **The PR body output the skill produces** MUST be UTF-8
   GitHub-Flavored Markdown. Use em dashes, smart punctuation,
   alerts, collapsibles, task lists, and Unicode where it improves
   readability. Mermaid diagram labels MAY use Unicode -- there is
   no constraint here. The output is consumed by GitHub's renderer,
   not by a Windows terminal.

A previous version of this skill incorrectly required ASCII in the
PR body. That made the output unreadable: no alerts, no collapsibles
for long evidence, no em dashes, no smart quotes. Reviewers had to
scroll through hundreds of flat lines instead of scanning a body
shaped by GFM features.

## Concision targets (hard ceilings)

The skill aims for **150-220 lines** for a typical PR body. **300+
lines is a smell, not a virtue**. If your draft exceeds 250 lines,
run a tightening pass: every sentence that does not change the
reviewer's understanding must be cut.

Per-section ceilings (enforced by `assets/section-rubric.md`):

| Section | Ceiling |
|---|---|
| TL;DR | 2-4 sentences |
| Problem (WHY) | max 6 bullets, max 3 quoted anchors total |
| Approach (WHAT) | a table OR 3-7 bullets; may be skipped if PR is purely additive (say "additive: see Implementation") |
| Implementation (HOW) | one short paragraph per file, OR a table; no prose walls |
| Diagrams | 1-3 mermaid blocks; every diagram preceded by a one-sentence legend |
| Trade-offs | 3-5 bullets; mechanical PRs may be 1-2 |
| Benefits | 3-5 numbered items, each measurable |
| Validation | copy-paste real command output; do not narrate |
| How to test | max 5 numbered steps |

Long verbatim quote blocks, full file listings, and full validation
transcripts SHOULD live inside `<details>` so the body stays
scannable.

## Core principles (with quoted anchors)

Each rule the skill enforces is backed by a verbatim quote from one
of the two reference docs. If a rule below cannot be backed by a
quote, it is downgraded to a "should" with the reason given.

1. **Self-sufficient body.** A reviewer must be able to read the PR
   body and form an opinion without opening any other doc, issue,
   or chat. Every WHY-claim cites the source doc inline; every
   named file is qualified with what changed in it; every diagram
   has a one-sentence legend.

   Anchor: Agent Skills,
   ["agents pattern-match well against concrete structures"](https://agentskills.io/skill-creation/best-practices).

2. **Anchored: every WHY-claim cites its source.** Every claim of
   the form "this violates X" or "this satisfies Y" is followed by
   a verbatim quoted phrase wrapped in a hyperlink to the source
   page. Reproduce quotes character-for-character; do not paraphrase
   inside link text.

   Anchor: PROSE,
   ["Grounding outputs in deterministic tool execution transforms probabilistic generation into verifiable action."](https://danielmeppiel.github.io/awesome-ai-native/docs/prose/).

3. **Cite-or-omit.** If a WHY-claim cannot be backed by a verbatim
   quote, drop it or soften to a tradeoff statement. Never invent
   justification.

   Anchor: Agent Skills,
   ["Add what the agent lacks, omit what it knows"](https://agentskills.io/skill-creation/best-practices).

4. **Visual aid where structure is non-trivial.** Any change that
   touches more than one file or alters control flow SHOULD include
   at least one mermaid diagram. Add a second only when the
   relationships are non-trivial. Never add a third unless it earns
   its place. Each diagram MUST be preceded by a one-sentence legend.

   Anchor: Agent Skills,
   ["agents pattern-match well against concrete structures"](https://agentskills.io/skill-creation/best-practices).

5. **Trade-offs explicit.** Address every non-obvious decision
   (option chosen vs option rejected). For mechanical PRs this
   section may be 1-2 bullets. For cross-cutting changes, surface
   the rejected alternatives.

   Anchor: PROSE,
   ["Favor small, chainable primitives over monolithic frameworks."](https://danielmeppiel.github.io/awesome-ai-native/docs/prose/).

6. **Single artifact, no fluff.** One markdown file. No marketing
   tone, no self-congratulation. TL;DR is at most four sentences.

   Anchor: Agent Skills,
   ["When you find yourself covering every edge case, consider whether most are better handled by the agent's own judgment."](https://agentskills.io/skill-creation/best-practices).

## GitHub-Flavored Markdown features the skill MUST use

The PR body is rendered by GitHub's Primer engine. Use the features
that engine provides; do not flatten the output to plain text.

- **Alerts** for high-signal callouts:
  `> [!NOTE]`, `> [!TIP]`, `> [!IMPORTANT]`, `> [!WARNING]`,
  `> [!CAUTION]`. Reference:
  https://github.com/orgs/community/discussions/16925.
- **Collapsible sections** for long diffs, full validation output,
  or appendix material:

  ```
  <details><summary>Full audit output</summary>

  ...content...
  </details>
  ```

  Use `<details open>` only when the content answers the most
  likely first reviewer question.
- **Task lists** for "How to test" sections:
  `- [ ] Apply label, observe X`.
- **Tables with alignment**: `| col | :---: | ---: |` for matrices.
- **Permalink references** to specific lines in the diff:
  `https://github.com/microsoft/apm/blob/<sha>/path#L12-L34`.

Long verbatim quote blocks, full file listings, and full validation
transcripts SHOULD live inside `<details>` so the body stays
scannable.

## Required body structure

| # | Section | Purpose |
|---|---------|---------|
| 1 | Title line | Imperative summary; first line `<verb>(<scope>): <summary>`, max 100 chars |
| 2 | TL;DR | 2-4 sentence executive summary |
| 3 | Problem (WHY) | Observed failure modes; max 6 bullets, max 3 quoted anchors |
| 4 | Approach (WHAT) | Table or 3-7 bullets; may say "additive: see Implementation" |
| 5 | Implementation (HOW) | One short paragraph per file or a table |
| 6 | Diagrams | 1-3 validated mermaid blocks, each with a legend |
| 7 | Trade-offs | 3-5 bullets (1-2 if mechanical) |
| 8 | Benefits | 3-5 numbered, measurable items |
| 9 | Validation | Real command output, ideally inside `<details>` if long |
| 10 | How to test | Max 5 numbered or task-list steps |

The Trade-offs (7) and How to test (10) sections are non-skippable
for any PR that changes more than docs.

## Activation contract -- inputs the orchestrator MUST gather first

Before invoking this skill, the orchestrator MUST have collected
all of the following. The skill MUST NOT invent facts not present
in these inputs.

| Input | Source | Required |
|-------|--------|----------|
| Branch name (head) | `git rev-parse --abbrev-ref HEAD` | yes |
| Base ref | usually `main`; ask if unclear | yes |
| List of files changed | `git diff --name-status <base>...HEAD` | yes |
| Actual diff | `git diff <base>...HEAD` | yes |
| Commit messages on the branch | `git log --no-merges <base>..HEAD --oneline` | yes |
| CHANGELOG entry, if any | inspect `CHANGELOG.md` Unreleased section | yes |
| Linked issue / motivation | user-provided or referenced in commits | yes |
| Validation evidence | output of `apm audit --ci`, `uv run pytest`, or equivalent | yes |
| Mirror parity check, if applicable | `apm install --target copilot` output | conditional |

If any required input is missing, the orchestrator MUST stop and
collect it. This is a Progressive Disclosure boundary:
["Context arrives just-in-time, not just-in-case."](https://danielmeppiel.github.io/awesome-ai-native/docs/prose/).
Do not load `assets/pr-body-template.md` until the table above is
complete.

## Execution checklist

Run these steps in order. Tick each before moving on.

1. [ ] Confirm every row of the activation contract is filled in.
2. [ ] Read the diff in full. Identify per-file change summary,
       new files, deleted files, behavior changes at module
       boundaries.
3. [ ] Load `assets/pr-body-template.md`. This is the only point
       at which the template enters context. Progressive Disclosure
       in action:
       ["store them in `assets/` and reference them from `SKILL.md` so they only load when needed."](https://agentskills.io/skill-creation/best-practices).
4. [ ] Fill in the template top-to-bottom using only facts from
       the activation contract. Every WHY-claim gets a verbatim
       quoted anchor. If you cannot anchor a claim, drop it.
5. [ ] Generate 1-3 mermaid diagrams. Add a one-sentence legend
       above each.
6. [ ] **Validate every mermaid block deterministically (see
       below). Do NOT save the draft until every block validates.**
7. [ ] Load `assets/section-rubric.md` and run the self-check pass.
       Validation loop pattern from Agent Skills:
       ["do the work, run a validator (a script, a reference checklist, or a self-check), fix any issues, and repeat until validation passes."](https://agentskills.io/skill-creation/best-practices).
8. [ ] Run the line-count check. If the body exceeds 250 lines,
       tighten until it fits 150-220.
9. [ ] Write the final body to a single file path provided by the
       orchestrator (default: `.git/PR_BODY.md` or
       session-state-relative). Return the path; do not paste the
       body inline unless explicitly asked.

## Mandatory mermaid validation step

Run every mermaid block in the draft through `mmdc` and refuse to
save until all pass.

```bash
# Extract mermaid blocks and validate each one.
# Requires: npx --yes -p @mermaid-js/mermaid-cli mmdc (one-shot, no global install needed)
awk '/^```mermaid/{n++; f=outdir"/diag"n".mmd"; getline; while($0 != "```") {print > f; getline}}' outdir=/tmp/mermaid-check pr-body-draft.md
for f in /tmp/mermaid-check/diag*.mmd; do
  npx --yes -p @mermaid-js/mermaid-cli mmdc -i "$f" -o "${f%.mmd}.svg" --quiet || { echo "INVALID: $f"; exit 1; }
done
```

If `mmdc` reports any error, fix the diagram and re-run. The skill
MUST NOT save the draft until every mermaid block validates.

### Common mermaid pitfalls

- **Semicolons in `classDiagram` link labels break the parser.** A
  label like `dispatches; verifies 3 artifacts` errors at the
  semicolon. Use commas or rephrase: `dispatches, verifies 3 artifacts`.
- **`note right of X` in `stateDiagram-v2`** must close with
  `end note` on its own line. A note that bleeds into the next
  state declaration is a parse error.
- **Round brackets `()` in flowchart node labels** need quoting:
  write `A["foo (bar)"]`, not `A[foo (bar)]`.
- **Double quotes inside node labels** must be HTML-escaped as
  `&quot;`. Mermaid does not support nested or escaped double
  quotes inside `"..."` labels.
- **Pipes `|` and angle brackets `<>` inside labels** also need
  quoting or HTML escapes; they are operators in mermaid syntax.
- **Edge labels with colons** can confuse `stateDiagram-v2`; prefer
  arrow-then-label form: `A --> B : trigger received`, not
  `A --> B[trigger: received]`.

## Output contract

- Exactly ONE markdown file is produced.
- The file is **UTF-8 GitHub-Flavored Markdown**. Em dashes, smart
  quotes, Unicode in mermaid labels, alerts, and collapsibles are
  all permitted and encouraged where they improve readability.
- Every mermaid block has been validated by `mmdc` and renders
  without error.
- The cite-or-omit rule applies absolutely.
- The TL;DR is at most four sentences.
- The body ends with the trailer:
  `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`

## Anti-patterns flagged -- refuse these

- **Posting unvalidated mermaid.** A parser error renders as raw
  code on GitHub and signals carelessness. Validate every block
  before saving.
- Pasting commit messages as the body. Commit messages are inputs,
  not output.
- Marketing tone or self-congratulation ("this is a great
  improvement", "significantly enhances", "best-in-class"). Strip
  on sight.
- Diagrams without a legend, OR diagrams that fail `mmdc`.
- A TL;DR longer than four sentences.
- Skipping any required section because "the PR is small". A small
  PR can have a one-line Implementation per file, but the section
  header must still be present.
- Restating the diff line-by-line in Implementation. That is what
  the Files Changed tab is for.
- Quoting a doc out of context. The self-check pass must verify
  that the quoted phrase actually supports the claim.
- **Forcing ASCII-only on the PR body.** That rule applies to
  source files and CLI output, not to Primer-rendered markdown.
  See "Output charset rule" above.

## Gotchas

- **Do not restate the diff.** Implementation is for intent, risk,
  and decisions -- not a textual re-rendering of the patch.
- **Do not quote out of context.** Re-read the surrounding paragraph
  of the source doc before pasting a quote.
- **Verify the source URL still serves the quoted text.** If the
  doc has been edited and the phrase no longer appears verbatim,
  drop the citation or find a new anchor.
- **A doc-only PR still needs TL;DR, Problem, Validation, and
  How-to-test.** "The PR is trivial" is not an exemption.
- **Long evidence belongs in `<details>`.** Reviewers should be
  able to read the whole body in a single screen-and-a-half scroll
  and expand evidence on demand.

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
