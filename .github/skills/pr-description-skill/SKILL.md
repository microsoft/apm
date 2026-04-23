---
name: pr-description-skill
description: >-
  Use this skill to write the PR description (PR body) for any pull
  request opened against microsoft/apm. Produces one self-sufficient
  markdown artifact containing TL;DR, Problem (WHY), Approach (WHAT),
  Implementation (HOW), mermaid diagrams, an honest PROSE alignment
  matrix, explicit trade-offs, validation evidence, and a How-to-test
  section -- with every WHY-claim backed by a verbatim quote from
  PROSE or Agent Skills. Activate when the user asks to "write a PR
  description", "draft a PR body", "open a PR", "fill in the PR
  template", or any equivalent.
model: claude-opus-4.7
---

# PR Description Skill -- Anchored, Self-sufficient PR Bodies

## When to use

Trigger this skill on any of the following intents:

- "write a PR description"
- "draft a PR body"
- "open a PR" / "open this PR" / "let's open the PR"
- "fill in the PR template"
- "summarize this branch as a PR"
- "create the PR write-up"

The skill is reusable for **any** PR against `microsoft/apm`. It is
not specialized to skill-bundle PRs, refactors, or any one subsystem.
The output is a single markdown file the orchestrator either pastes
into `gh pr create --body-file` or surfaces to the maintainer.

## Core principles (with quoted anchors)

Each rule the skill enforces is backed by a verbatim quote from one
of the two reference docs. If a rule below cannot be backed by a
quote, it is downgraded to a "should" with the reason given.

1. **Self-sufficient body.** A reviewer must be able to read the PR
   body and form an opinion without opening any other doc, issue, or
   chat. Concretely: every WHY-claim cites the source doc inline,
   every named file is qualified with what changed in it, and every
   diagram has an ASCII-only legend.

   Anchor: Agent Skills,
   ["agents pattern-match well against concrete structures"](https://agentskills.io/skill-creation/best-practices).
   A self-sufficient body IS the concrete structure the reviewer
   pattern-matches against.

2. **Anchored: every WHY-claim cites its source.** Every claim of the
   form "this violates X" or "this satisfies Y" must be followed by a
   verbatim quoted phrase wrapped in a hyperlink to the source page.
   Quotes are reproduced character-for-character; do not paraphrase
   inside the link text.

   Anchor: PROSE,
   ["Grounding outputs in deterministic tool execution transforms probabilistic generation into verifiable action."](https://danielmeppiel.github.io/awesome-ai-native/docs/prose/).
   A reviewer following the link IS the verification step.

3. **Cite-or-omit.** If a WHY-claim cannot be backed by a verbatim
   quote, the claim is dropped or softened to a tradeoff statement.
   Never invent justification. Never paraphrase a doc and present the
   paraphrase as a quote.

   Anchor: Agent Skills,
   ["Add what the agent lacks, omit what it knows"](https://agentskills.io/skill-creation/best-practices).
   The reviewer already knows generic best practices; only project-
   specific or doc-specific anchors add value.

4. **Visual aid where structure is non-trivial.** Any change that
   touches more than one file, introduces a new control flow, or
   alters a state machine MUST include at least one mermaid diagram
   (`flowchart`, `stateDiagram-v2`, or `classDiagram`). All node
   labels, edge labels, and notes are ASCII-only.

   Anchor: Agent Skills,
   ["agents pattern-match well against concrete structures"](https://agentskills.io/skill-creation/best-practices).
   A diagram is the most pattern-matchable structure for code-shape
   change.

5. **Honest alignment matrix.** When the PR claims to advance a
   PROSE dimension (Progressive Disclosure, Reduced Scope,
   Orchestrated Composition, Safety Boundaries, Explicit Hierarchy),
   the matrix MUST show "Before" and "After" cells AND a 1-5 score.
   Scores below 5 require a one-sentence reason naming what is still
   missing. A row of all-5s without explicit "why not 4" justification
   is refused as inflation.

   Anchor: PROSE,
   ["Match task size to context capacity."](https://danielmeppiel.github.io/awesome-ai-native/docs/prose/).
   An honest matrix keeps the PR scope visible; an inflated matrix
   hides residual scope.

6. **Trade-offs explicit.** Every non-obvious decision (option chosen
   vs option rejected) appears in a Trade-offs / self-critique
   section, including the rationale grounded in a quote when
   possible. This includes scope decisions ("we did not also fix X
   because ...").

   Anchor: PROSE,
   ["Favor small, chainable primitives over monolithic frameworks."](https://danielmeppiel.github.io/awesome-ai-native/docs/prose/).
   Surgical scope IS the small primitive; trade-offs document why
   the PR did not balloon.

7. **Single artifact, no fluff.** The output is one markdown file.
   No marketing tone, no "this is a great improvement", no
   self-congratulation. The TL;DR is at most four sentences.

   Anchor: Agent Skills,
   ["When you find yourself covering every edge case, consider whether most are better handled by the agent's own judgment."](https://agentskills.io/skill-creation/best-practices).
   Brevity is itself a discipline against context bloat for the
   reviewer.

## Required body structure

The PR body MUST follow this section order. Each section has a one-
line purpose and a one-line acceptance test. Full per-section rubric
lives in `assets/section-rubric.md` and is loaded only at the self-
check step.

| # | Section | Purpose | Acceptance test |
|---|---------|---------|-----------------|
| 1 | Title line | One imperative summary of the change. | First line is `# <verb>(<scope>): <summary>` and is at most 100 chars. |
| 2 | TL;DR | Four-sentence-max executive summary. | `len(sentences) <= 4`. |
| 3 | Problem (WHY) | Bulleted observed failure modes; each tagged `[x]` or `[!]`. | At least 2 verbatim quotes from PROSE or Agent Skills. |
| 4 | Approach (WHAT) | Numbered table of fixes mapped to a quoted principle and source doc. | Every row has a Principle column AND a Source column. |
| 5 | Implementation (HOW) | Per-file subsections describing what changed and why. | Every named file appears as an `H3` subsection with at least one quoted anchor. |
| 6 | Diagrams | At least one mermaid diagram for non-trivial PRs. | Every node and edge label is ASCII; at least one note or legend. |
| 7 | PROSE alignment matrix | "Before / After / 1-5 score" per PROSE dimension touched. | Any score < 5 has a "why not 5" sentence; any all-5 column has explicit justification. |
| 8 | Trade-offs and self-critique | Option chosen vs option rejected, with rationale. | At least one rejected option per non-trivial decision. |
| 9 | Benefits (recap) | Numbered, concrete, no marketing tone. | No adjectives like "great", "amazing", "significantly". |
| 10 | Validation | Concrete commands run + output excerpts. | At least one fenced block of real CLI output. |
| 11 | How to test | Numbered reproducible steps a reviewer can follow. | Every step is independently runnable; no "see the diff". |

The Trade-offs (8) and How to test (11) sections are non-skippable
for any PR that changes more than docs.

## Activation contract -- inputs the orchestrator MUST gather first

Before invoking this skill, the orchestrator MUST have collected all
of the following. The skill MUST NOT invent facts not present in
these inputs.

| Input | Source | Required |
|-------|--------|----------|
| Branch name (head) | `git rev-parse --abbrev-ref HEAD` | yes |
| Base ref | usually `main`; ask if unclear | yes |
| List of files changed | `git diff --name-status <base>...HEAD` | yes |
| Actual diff | `git diff <base>...HEAD` (or path to a saved diff) | yes |
| Commit messages on the branch | `git log --no-merges <base>..HEAD --oneline` | yes |
| CHANGELOG entry, if any | inspect `CHANGELOG.md` Unreleased section | yes |
| Linked issue / motivation | user-provided or referenced in commits | yes |
| Validation evidence | output of `apm audit --ci`, `uv run pytest`, or equivalent | yes |
| Mirror parity check, if applicable | `apm install --target copilot` output | conditional |

If any required input is missing, the orchestrator MUST stop and
collect it before loading the template. This is a Progressive
Disclosure boundary:
["Context arrives just-in-time, not just-in-case."](https://danielmeppiel.github.io/awesome-ai-native/docs/prose/).
Do not load `assets/pr-body-template.md` until the table above is
complete.

## Execution checklist

Run these steps in order. Tick each before moving on.

1. [ ] Confirm every row of the activation contract is filled in.
       If a row is missing, stop and ask the user or run the
       collection command. Do NOT proceed on assumption.
2. [ ] Read the diff in full. Identify: (a) per-file change summary,
       (b) any new file, (c) any deleted file, (d) any change in
       behavior at module boundaries.
3. [ ] Decide which PROSE dimensions the change touches (zero or
       more of: Progressive Disclosure, Reduced Scope, Orchestrated
       Composition, Safety Boundaries, Explicit Hierarchy). If none,
       the alignment matrix may be omitted -- record that decision
       in Trade-offs.
4. [ ] Load `assets/pr-body-template.md`. This is the only point in
       the run at which the template is brought into context. This
       is Progressive Disclosure in action:
       ["store them in `assets/` and reference them from `SKILL.md` so they only load when needed."](https://agentskills.io/skill-creation/best-practices).
5. [ ] Fill in the template top-to-bottom using only facts from the
       activation contract inputs. Every WHY-claim gets a verbatim
       quoted anchor. If you cannot anchor a claim, drop it.
6. [ ] Generate at least one mermaid diagram for any non-doc-only
       PR. Verify ASCII purity of node and edge labels before
       moving on.
7. [ ] Load `assets/section-rubric.md` and run the self-check pass.
       For each section, run the 1-line acceptance test against
       your own draft. This is the validation loop pattern from
       Agent Skills:
       ["do the work, run a validator (a script, a reference checklist, or a self-check), fix any issues, and repeat until validation passes."](https://agentskills.io/skill-creation/best-practices).
8. [ ] Run an ASCII-purity check on the final draft (printable
       U+0020-U+007E plus `\n` and `\t` only). Refuse to save if any
       character falls outside this range.
9. [ ] Write the final body to a single file path provided by the
       orchestrator (default: `.git/PR_BODY.md` or
       session-state-relative path). Return the path; do not paste
       the body inline unless explicitly asked.

## Output contract

- Exactly ONE markdown file is produced. No multi-file output, no
  inline echo unless the user explicitly asks.
- The file is ASCII-only (printable U+0020 through U+007E plus
  newline and tab). No emojis, no em dashes, no curly quotes, no
  box-drawing characters.
- Every mermaid label, note, and legend is ASCII.
- The cite-or-omit rule applies absolutely: if no verbatim quote
  backs a "this violates X" or "this satisfies Y" claim, the claim
  is dropped or rewritten as an explicit trade-off.
- The TL;DR is at most four sentences.
- The body ends with the standard trailer:
  `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`

## Anti-patterns flagged -- refuse these

The skill MUST refuse to produce a body that exhibits any of the
following:

- Pasting commit messages as the body. Commit messages are inputs,
  not output.
- Marketing tone or self-congratulation ("this is a great
  improvement", "significantly enhances", "best-in-class"). Strip
  on sight.
- Unsupported alignment scores. A column reading 5/5/5/5/5 with no
  justification is refused; the maintainer's heuristic is "all
  fives means the author did not look hard enough".
- Diagrams without legends, OR diagrams whose labels contain any
  non-ASCII character.
- A TL;DR longer than four sentences.
- Skipping any required section because "the PR is small". A small
  PR can have a one-line Implementation subsection per file, but
  the section header must still be present.
- Restating the diff line-by-line in the Implementation section.
  Implementation describes intent and risk per file, not text the
  reviewer can read in the diff viewer.
- Quoting a doc out of context (cherry-picking a phrase whose
  surrounding sentences contradict the use here). The self-check
  pass must verify that the quoted phrase actually supports the
  claim.

## Gotchas

These are environment-specific traps based on the worked example
this skill was extracted from. Read them before starting any draft.

- **Do not restate the diff.** The Implementation section is for
  intent, risk, and which decisions were made -- not a textual
  re-rendering of the patch. Reviewers can open the Files Changed
  tab.
- **Do not quote out of context.** Always re-read the surrounding
  paragraph of the source doc before pasting a quote. A phrase that
  reads as universal ("Match task size to context capacity") may
  appear in a section that constrains its scope.
- **Verify the source URL still serves the quoted text.** If the
  doc has been edited, the link may now point to a page where the
  quoted phrase no longer appears. The cite-or-omit rule applies:
  if you cannot find the phrase verbatim at the linked URL, drop
  the citation and rephrase the claim, or find a different anchor.
- **ASCII purity bites silently.** A single em dash from an
  autocorrect-style paste will pass visual review but fail the
  cross-platform encoding rule. Run the purity check before saving.
- **Mermaid label characters are a common ASCII trap.** Avoid `->`
  inside node labels (mermaid will parse it); prefer `to` or `-->`
  on the edge itself. Avoid parentheses-with-quotes patterns inside
  labels; mermaid quoting rules differ across renderers.
- **The alignment matrix tempts inflation.** If you score a
  dimension at 5/5, double-check that the change actually moves
  ALL of: the source-of-truth file, every dependent reference, and
  the gotcha that drove the failure mode. Score 4 with a clear
  "why not 5" sentence is more credible than an unjustified 5.
- **A doc-only PR still needs a TL;DR, a Problem section, a
  Validation section, and a How-to-test section.** "The PR is
  trivial" is not an exemption; a one-line Problem and a one-step
  How-to-test are sufficient.

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
