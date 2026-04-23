<!--
  section-rubric.md

  Loaded by pr-description-skill ONLY at the self-check step (step 7
  of the execution checklist). For each section the orchestrator
  drafted, run the matching block below as a "fresh eyes" pass: read
  the section, run the acceptance test, fix or rewrite if it fails,
  re-run until it passes. Then proceed to the next section.

  ASCII-only.
-->

# Section Rubric -- Per-section Quality Bar and Self-check

## 1. Title line

- **Purpose**: One imperative summary the reviewer reads in the PR
  list view.
- **Minimum content**: `<verb>(<scope>): <summary>`. Verb is one of
  add, fix, refactor, harden, document, ship, remove, deprecate.
  Scope matches the area touched (cli, install, integration, docs,
  ci, skills, etc.).
- **Failure modes to refuse**:
  - Past tense ("added", "fixed").
  - Title longer than 100 chars.
  - Scope that names a single file ("update SKILL.md") instead of
    an area.
- **Acceptance test**: First line starts with one of the listed
  verbs, contains a parenthesized scope, and is at most 100 chars.

## 2. TL;DR

- **Purpose**: Executive summary that lets a busy reviewer triage
  the PR in under 30 seconds.
- **Minimum content**: What changed, why now, the risk eliminated.
  Four sentences maximum.
- **Failure modes to refuse**:
  - More than four sentences.
  - Marketing tone ("this is a great improvement").
  - Restating the title in different words without adding the
    "why now" or the "eliminated risk".
- **Acceptance test**: Count sentences. If `count > 4`, rewrite. If
  any sentence contains an adjective in {"great", "amazing",
  "significantly", "best-in-class", "powerful"}, strip it.

## 3. Problem (WHY)

- **Purpose**: Convince the reviewer that the change is necessary
  by showing observed failure modes today, not hypothetical future
  ones.
- **Minimum content**:
  - At least two bullet items tagged with `[x]` (hard violation) or
    `[!]` (soft risk).
  - At least two verbatim quoted anchors from PROSE or Agent Skills,
    each wrapped in a hyperlink to the source URL.
  - Each anchor cites the failure mode it explains, not a generic
    principle.
- **Failure modes to refuse**:
  - Hypothetical-only language ("could lead to", "might cause")
    with no observed evidence.
  - Quotes paraphrased inside the link text instead of reproduced
    verbatim.
  - Anchors to anything other than PROSE or Agent Skills (or
    another canonical reference the orchestrator has been asked to
    use).
- **Acceptance test**: Count anchored quotes. If `count < 2`, find
  more anchors or drop unsupported claims.

## 4. Approach (WHAT)

- **Purpose**: Show the reviewer the shape of the fix in one
  scannable table before they read the prose.
- **Minimum content**: A table with columns `#`, `Fix`,
  `Principle operationalized`, `Source`. Every row has all four
  columns filled. Every Principle cell is a verbatim quote with a
  hyperlink. Every Source cell names PROSE constraint or Agent
  Skills section.
- **Failure modes to refuse**:
  - Rows with empty Principle or Source.
  - Paraphrased principles ("basically Progressive Disclosure")
    instead of verbatim quotes.
  - One mega-row covering "everything in this PR"; rows must
    decompose to surgical fixes.
- **Acceptance test**: For each row, the Principle cell contains a
  pair of double quotes around a string that appears verbatim at
  the linked URL.

## 5. Implementation (HOW)

- **Purpose**: Tell the reviewer per file: what changed in intent,
  what risk it carries, what was deliberately NOT touched.
- **Minimum content**:
  - One H3 subsection per file changed (or per logical group of
    files when the change is uniform).
  - Bullets describing intent, risk, and surgical-scope notes.
  - At least one quoted anchor per subsection, OR an explicit note
    "no anchor needed -- mechanical change".
- **Failure modes to refuse**:
  - Line-by-line restatement of the diff.
  - "Refactored for clarity" with no specific intent.
  - Files mentioned only in the title or TL;DR with no H3
    subsection.
- **Acceptance test**: For each file in the activation contract's
  changed-files list, an H3 subsection with the file path in
  backticks exists. If not, add it or explain its absence.

## 6. Diagrams

- **Purpose**: Make non-trivial structural change pattern-matchable
  at a glance.
- **Minimum content**:
  - At least one mermaid block for any PR that touches more than
    one file or alters control flow.
  - One ASCII legend per diagram, even if obvious.
  - Diagram type matched to content: `flowchart` for control flow,
    `stateDiagram-v2` for lifecycle, `classDiagram` for artifact
    relationships.
- **Failure modes to refuse**:
  - Any non-ASCII character in a node label, edge label, or note.
  - Diagrams with no legend.
  - Decorative diagrams that do not reflect the change (e.g. a
    flowchart of unchanged code).
- **Acceptance test**: Run an ASCII check on the contents of every
  fenced mermaid block. Verify the diagram references at least one
  file or function actually touched by the diff.

## 7. PROSE alignment matrix

- **Purpose**: Force an honest before/after assessment for every
  PROSE dimension the change touches.
- **Minimum content**:
  - One row per PROSE dimension touched (Progressive Disclosure,
    Reduced Scope, Orchestrated Composition, Safety Boundaries,
    Explicit Hierarchy).
  - Columns: dimension name, Before state, After state, 1-5 score.
  - Any score below 5 is followed by a one-sentence "why not 5"
    sentence after the table.
  - Any score of 5 is justified by naming the source-of-truth file,
    every dependent reference, and the gotcha resolved.
- **Failure modes to refuse**:
  - All-5s without justification. The maintainer rule of thumb:
    "all fives means the author did not look hard enough".
  - Empty Before or After cells.
  - Dimensions claimed without any evidence in the diff.
- **Acceptance test**: For each row, ask: "Could a reviewer reading
  the diff agree with this Before/After characterization?" If not,
  rewrite the cells before adjusting the score.

## 8. Trade-offs and self-critique

- **Purpose**: Show the reviewer that obvious alternatives were
  considered and rejected with reasons, not missed.
- **Minimum content**:
  - At least one bullet per non-trivial decision.
  - Each bullet has the shape: option chosen, option rejected,
    rationale, anchor (when possible).
  - Surgical-scope decisions ("we did not also fix X because ...")
    are listed here, not hidden in Implementation.
- **Failure modes to refuse**:
  - Trade-offs that read like benefits in disguise ("we chose to
    do this because it is better").
  - "No trade-offs" claim. Every non-trivial PR has at least one
    rejected option; if none surface, push back.
  - Rationale that boils down to "personal preference" with no
    grounding.
- **Acceptance test**: For each bullet, the words "rejected:" or
  "option rejected" appear, followed by a concrete alternative.

## 9. Benefits (recap)

- **Purpose**: Let the reviewer confirm that the WHY in section 3
  is actually addressed by the WHAT in section 4.
- **Minimum content**: Numbered list of concrete, observable
  benefits. Each benefit names something a reviewer can verify
  (count of comments emitted, presence of a section, behavior
  under a specific input).
- **Failure modes to refuse**:
  - Adjectives in {"great", "amazing", "significantly",
    "best-in-class", "powerful", "robust"}.
  - Benefits that restate the fix without naming the observable
    outcome.
- **Acceptance test**: For each benefit, ask: "What command or
  observation lets the reviewer verify this?" If you cannot
  answer, rewrite or drop the bullet.

## 10. Validation

- **Purpose**: Show the reviewer that the change has been
  exercised, not just written.
- **Minimum content**:
  - At least one fenced code block of real CLI output (`apm audit
    --ci`, `uv run pytest`, `apm install --target copilot`, or
    equivalent).
  - An ASCII-purity statement listing each authored / rewritten
    file with `OK` or a documented exception.
  - When applicable, a mirror-parity check (`apm install --target
    copilot` confirms `.apm/` and `.github/` are in sync).
- **Failure modes to refuse**:
  - Invented or stylized output ("[+] all tests pass" with no
    real command shown).
  - Skipping ASCII purity for any authored file.
  - Output excerpts that hide failures with `...`.
- **Acceptance test**: For each fenced output block, the command
  that produced it is named on the immediately preceding line, and
  the output is verbatim (warts included).

## 11. How to test

- **Purpose**: Give the reviewer a reproducible script they can
  follow without reading the source.
- **Minimum content**: Numbered steps. Each step has an action and
  an expected observation. Steps cover both the happy path and at
  least one edge case the change addresses.
- **Failure modes to refuse**:
  - "See the diff" or "obvious from the code" as a step.
  - Steps that depend on un-mentioned setup.
  - Steps that rely on a private fixture or unshipped data.
- **Acceptance test**: Pick a hypothetical reviewer who has never
  seen this branch. Can they follow the steps end-to-end with only
  what is in the PR body and the repo? If not, rewrite.

## Final pass -- run before saving

- [ ] All eleven sections present (10 if alignment matrix omitted
      AND that omission is documented in Trade-offs).
- [ ] No `<PLACEHOLDER>`, `TBD`, or `TODO` strings remain.
- [ ] Every quote in the body appears verbatim at its linked URL
      (spot-check at least 3 quotes by re-fetching the page).
- [ ] ASCII purity confirmed for the body file (printable
      U+0020-U+007E plus newline and tab).
- [ ] TL;DR sentence count is 4 or fewer.
- [ ] At least one mermaid diagram is present for any non-doc-only
      PR, and every diagram label is ASCII.
- [ ] Trailer line `Co-authored-by: Copilot
      <223556219+Copilot@users.noreply.github.com>` is the last
      non-empty line of the file.
