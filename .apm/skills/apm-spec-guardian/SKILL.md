---
name: apm-spec-guardian
description: >-
  Use this skill to run a four-panel adversarial advisory review on any
  pull request that touches the OpenAPM specification artifact
  (docs/src/content/docs/specs/openapm-*.md), its inline / sidecar JSON
  Schemas (docs/src/content/docs/specs/schemas/*.schema.json), or the
  conformance fixture seed (tests/fixtures/spec-conformance/**). The
  panel fans out to four spec-ecosystem reviewers
  (swagger-openapi-editor, oci-distribution-editor,
  pkgmgr-registry-contract-editor, w3c-tag-architect), each running in
  its own agent thread, and a spec-editor synthesizer that produces a
  fold-now / defer-v0.1.1 / defer-v0.2 / reject list plus a ship
  decision keyed off a 1..10 shocked_meter scale. The orchestrator is
  the sole writer to the PR: ONE consolidated comment, no verdict
  labels, no merge gating. The panel is advisory -- it surfaces
  findings, prioritizes folds, and renders a ship recommendation that
  the maintainer weighs.
---

# APM Spec Guardian -- Four-Panel Advisory Review for OpenAPM

This skill institutionalizes the two-round adversarial spec review
that produced OpenAPM v0.1 by hand. The panel is FAN-OUT +
SYNTHESIZER. Each panelist runs in its own agent thread (via the
`task` tool) and returns JSON matching
`assets/panelist-return-schema.json`. The orchestrator schema-validates
each return, hands all returns to the `spec-editor-synthesizer`
(also a task thread, returns JSON matching
`assets/synthesizer-return-schema.json`), runs the linter checklist
in `assets/linter-checklist.md`, then renders ONE comment from
`assets/comment-template.md`.

This skill is ADVISORY by design. It does not compute a binary
verdict, it does not apply verdict labels, and it does not gate
merge. The panel surfaces findings; the maintainer ships.

## Activation scope

This skill activates ONLY when the PR diff touches at least one of:

- `docs/src/content/docs/specs/openapm-*.md` (the normative spec
  artifact, current and future versions)
- `docs/src/content/docs/specs/schemas/*.schema.json` (sidecar JSON
  Schemas, if/when the inline Appendix-A schemas are extracted to
  files)
- `tests/fixtures/spec-conformance/**` (the conformance fixture seed)

Edits to any OTHER documentation page MUST NOT trigger this skill.
The maintainer's general `docs-sync` skill covers those.

## Architecture invariants

- **Advisory regime, not gate regime.** There is no `APPROVE` /
  `REJECT`, no `spec-approved` / `spec-rejected` label, no
  deterministic verdict computation. The synthesizer returns a
  `ship_decision` (`fold_and_ship` / `needs_revision` /
  `next_brief`); this is prose for the human reviewer, never
  auto-applied as a label or status check.
- **Ship-meter floor.** `ship_decision: fold_and_ship` REQUIRES
  `shocked_meter_avg >= 7.0`. Below 7.0 the synthesizer MUST emit
  `needs_revision` (single drafter pass on the existing artifact)
  or `next_brief` (another round of panel review with a new brief).
  The floor is advisory wording in the comment, not a status check.
- **Blocker veto.** If ANY panelist returns
  `new_blocking_findings[].length > 0`, the synthesizer MUST emit
  `ship_decision: next_brief` regardless of the shocked_meter_avg.
  A blocking finding from one panel is not outweighed by three
  panels rating the artifact 9/10.
- **Single-writer interlock.** Only the orchestrator writes to the
  PR: exactly one `add-comment` call and one `remove-labels` call.
  The `remove-labels` call sweeps `spec-review` (trigger
  idempotency). NO `add-labels` call -- there are no verdict labels.
  Panelist subagents and the synthesizer subagent return JSON only
  and MUST NOT call any `gh` write command, post comments, apply
  labels, or touch PR state.
- **Single-emission discipline.** Exactly one comment per panel run,
  rendered from `assets/comment-template.md` after all subagents
  return and the linter checklist runs.
- **ASCII-only artifact.** Every byte the skill writes (the comment,
  the synthesizer prose, any rendered fold instruction) MUST be
  within printable ASCII (U+0020 - U+007E). The skill inherits the
  repo encoding rule from `.github/instructions/encoding.instructions.md`
  (if present) and additionally enforces it on the spec artifact via
  linter check 1.
- **No-vendor-foundation language ban.** The spec artifact MUST NOT
  contain "CNCF", "Linux Foundation", "Sandbox", "Incubation",
  "W3C Process", or "IETF RFC stream". The persona prompts MAY
  reference these as pedigree (a panelist's credibility comes from
  having edited OpenAPI; that does not put OpenAPI's foundation
  affiliation in the spec text). Linter check 2 greps the artifact
  for the forbidden token list AFTER any fold.

## Agent roster

| Agent | Role | Always active? |
|-------|------|----------------|
| [Swagger / OpenAPI Editor](../../agents/spec-swagger-editor.agent.md) | Interface-contract discipline (schemas, $ref hygiene, oneOf discriminators, conformance enumeration) | Yes |
| [OCI Distribution Editor](../../agents/spec-oci-editor.agent.md) | Registry-HTTP rigor (hash envelopes, mirror tolerance, fail-closed extraction, supply-chain threat model) | Yes |
| [Package-Manager Registry-Contract Editor](../../agents/spec-pkgmgr-editor.agent.md) | Dependency-resolution rigor (semver dialect pinning, lockfile determinism, transitive conflict policy, reserved-slot defensive MUSTs) | Yes |
| [W3C TAG Architect](../../agents/spec-tag-architect.agent.md) | Web-platform integration / architecture (extensibility, layering, fingerprinting, machine-readable contract surface) | Yes |
| [Spec Editor Synthesizer](../../agents/spec-editor-synthesizer.agent.md) | Same hand that drafted; aggregates panel returns, computes shocked_meter_avg, clusters convergent themes, produces fold-now / defer / reject lists and ship_decision | Yes |

The roster is invariant for the v0.1 lineage. Changing it requires
bumping the skill version.

## Topology

```
   apm-spec-guardian SKILL (orchestrator thread)
                      |
   +------ Wave 0: scope decision (orchestrator-internal) ------+
   |  classify diff:                                            |
   |   - editorial-only (tiny patch, single section, no schema  |
   |     change, no fixture add)  -> skip Wave 3, run Wave 5    |
   |     linter only, render lightweight comment                |
   |   - editorial-patch (default for PR-trigger)               |
   |     -> skip Wave 1+2, start at Wave 3                      |
   |   - new-version (operator opt-in via PR body marker        |
   |     `apm-spec-guardian: new-version`)                      |
   |     -> run Wave 1 + 2 + 3 + 4 + 5                          |
   +------------------------------------------------------------+
                      |
        IF new-version mode: run Wave 1 + Wave 2 first
                      |
   Wave 1 (optional, new-version only): task -> spec-editor-synthesizer
          acting as ASSESSOR -- reads issue context + corpus +
          produces SPEC_BRIEF_v0 (session-state artifact)
   Wave 2 (optional, new-version only): task -> spec-editor-synthesizer
          acting as DRAFTER -- produces SPEC_DRAFT_v0 from SPEC_BRIEF_v0
                      |
   Wave 3: FAN-OUT via task tool (4 panelists in parallel)
   +------+------+--------+------+
   v      v      v        v
   swagger oci  pkgmgr   tag
   (each returns JSON per assets/panelist-return-schema.json)
                      |
                      v   <-- S4 schema-validate per return
                      v   <-- on malformed: re-spawn that panelist
                      v       (max 2 attempts; then placeholder)
                      v
   Wave 4: task -> spec-editor-synthesizer
   - aggregates findings across 4 panels
   - computes shocked_meter_avg
   - resolves dissent
   - clusters into convergent themes
   - emits fold_now[] + defer_v0_1_1[] + defer_v0_2[] + reject[]
   - emits ship_decision honoring ship-meter floor + blocker veto
   - returns assets/synthesizer-return-schema.json
                      |
                      v   <-- S4 schema-validate
                      v
   Wave 5: LINTER (mechanical, from assets/linter-checklist.md)
          - 11 checks; each MUST pass
          - failures append to ship_prose as advisory notes
          - linter does NOT change ship_decision; it informs the
            human reviewer
                      |
   Wave 6: orchestrator (sole writer)
            |               |
            v               v
        add-comment    remove-labels
        (max:1)        [spec-review]
                       (trigger idempotency reset)
```

## Wave 0 -- scope decision

The orchestrator classifies the diff before spawning any panelist.
Decision rules, in order:

1. **New-version mode.** If the PR body contains the literal marker
   line `apm-spec-guardian: new-version`, OR the diff creates a new
   `docs/src/content/docs/specs/openapm-*.md` file (not an edit to
   an existing one), classify as `new-version`. Run Waves 1 -> 2 ->
   3 -> 4 -> 5 -> 6.
2. **Editorial-only mode.** If ALL of the following hold:
   - the diff added < 50 lines AND removed < 50 lines total across
     all in-scope paths,
   - no JSON Schema file was added, removed, or had its top-level
     `properties` keys changed,
   - no fixture file was added or removed (existing fixture
     content edits are OK),
   - no anchor of the form `<a id="req-` was added or removed,
   classify as `editorial-only`. SKIP Wave 3 + Wave 4. Run Wave 5
   linter on the modified artifact. Render the lightweight
   editorial-only branch of `assets/comment-template.md` (just the
   linter result + a one-line "no substantive spec change
   detected").
3. **Editorial-patch mode (default).** Everything else. Run Wave 3
   -> 4 -> 5 -> 6. Wave 1 + Wave 2 are SKIPPED; the panel reviews
   the existing artifact as modified by the PR diff.

Document the decision in the comment header (one line: "Scope: <mode>;
diff = +X/-Y lines across N files").

## Wave 3 -- panel fan-out

Spawn the following four tasks in PARALLEL via the `task` tool, one
task per persona:

- `spec-swagger-editor`
- `spec-oci-editor`
- `spec-pkgmgr-editor`
- `spec-tag-architect`

Each task prompt MUST:

- Reference its persona file by relative path
  (`../../agents/spec-<slug>.agent.md`) so the subagent loads its
  own scope, lens, and pedigree.
- Include the PR number, title, body, and full diff (passed inline),
  PLUS the current contents of the in-scope spec artifact AS
  MODIFIED by the diff (the panel reviews the post-merge state of
  the file, not just the diff).
- Cite `assets/panelist-return-schema.json` and require the
  subagent to emit JSON matching that schema as its FINAL message.
- State the calibrated severity contract: "Use
  `new_blocking_findings` ONLY for issues that would break a
  conformant implementation, leak a security guarantee, or invalidate
  a published normative claim. Use `new_recommended_findings` for
  substantive improvements. Use `new_nit_findings` for one-line
  editorial polish. The panel is advisory; nothing you return blocks
  merge; pick the severity that honestly matches your signal
  strength."
- State the no-vendor-foundation rule: "Your pedigree as a [persona]
  is part of your prompt. Do NOT propose adding the names of any
  standards body, foundation, or governance program (CNCF, Linux
  Foundation, Sandbox, Incubation, W3C Process, IETF RFC stream) to
  the spec artifact text. Findings that recommend such additions
  will be auto-rejected by the synthesizer."
- State the ASCII rule: "Every byte in your return JSON and every
  byte of any proposed fix or replacement text MUST be within U+0020
  - U+007E. No emojis, no Unicode dashes, no curly quotes."
- Pass `round` (1 on first pass, 2+ on subsequent rounds in
  new-version mode).
- Restate the output contract: NO `gh` write commands, NO posting
  comments, NO label changes, NO touching PR state. JSON return only.

## Wave 4 -- synthesizer

Pass all four validated panelist JSON returns to a `task` invocation
that loads `../../agents/spec-editor-synthesizer.agent.md`. The prompt
MUST:

- Provide all panelist returns as structured input.
- Ask for: `convergence_table`, `convergent_themes` (themes flagged
  by 2+ panels), `fold_now[]` (surgical single-section fixes only),
  `defer_v0_1_1[]` (small patches deferrable to next patch
  release), `defer_v0_2[]` (architectural work requiring a reserved
  slot in a future major), `reject[]` (findings the synthesizer
  declines, with rationale), `ship_decision`, `ship_prose`,
  `linter_handoff_notes`.
- State the ship-decision rules verbatim:
  - If `sum(panelist.new_blocking_findings)` > 0 across all panels,
    `ship_decision` MUST be `next_brief`.
  - Else if `shocked_meter_avg < 7.0`, `ship_decision` MUST be
    `needs_revision`.
  - Else `ship_decision` MAY be `fold_and_ship`.
- State the no-vendor-foundation rule: auto-reject any
  panelist-proposed fix that would add a banned token to the
  artifact; surface in `reject[]` with rationale.
- Cite `assets/synthesizer-return-schema.json` and require JSON
  return.
- Restate the contract: the panel is advisory. The synthesizer does
  NOT pick a verdict label. The `ship_decision` is prose for the
  human reviewer, not a gate. NO `gh` write commands.

Validate the synthesizer return against
`assets/synthesizer-return-schema.json`. On failure, re-spawn once
with the violation cited.

## Wave 5 -- linter

Run `assets/linter-checklist.md` against the in-scope artifact set
(spec markdown + schemas + fixtures). The checklist has 11 mechanical
checks; each is a one-liner producing exit code 0 (or empty grep
output where noted). Record pass / fail per check.

Linter outcomes are ADVISORY: a failed check does NOT change the
synthesizer's `ship_decision`. It DOES surface in the comment as a
"Linter notes" section so the maintainer can decide whether to fold
the fix into the same PR. If `ship_decision == fold_and_ship` AND
any linter check failed, the comment surfaces the conflict
prominently ("Synthesizer recommends ship; linter found N issues
worth folding first").

## Wave 6 -- render the comment

Load `assets/comment-template.md`, fill the placeholders from the
synthesizer + panelist JSON + linter results, and emit it as exactly
ONE comment.

Filling rules:

- The convergence table renders ONE row per panelist with verdict,
  shocked_meter, new_blockers, new_recommended, new_nits counts.
- The fold-now list renders the synthesizer's `fold_now[]`
  verbatim, ordered as returned.
- The defer-v0.1.1 and defer-v0.2 lists render below the fold list,
  collapsed in `<details>` blocks if either has more than 3 items.
- The reject list renders only if non-empty.
- The "Linter notes" section renders only if any check failed; each
  failed check renders the check id + the one-line failure summary.
- Full per-panel findings collapse into a `<details>` at the bottom.
- NEVER render the words "Verdict", "APPROVE", "REJECT", "blocked",
  "merge gate", or any equivalent. The panel is advisory.

Then sweep the `spec-review` label via `safe-outputs.remove-labels`
(idempotent on missing labels). NO `add-labels` call.

## Output contract (non-negotiable)

- Exactly ONE comment per panel run, rendered from
  `assets/comment-template.md`.
- Exactly ONE `remove-labels` call sweeping `[spec-review]`.
- NO `add-labels` call.
- Subagents (panelists + synthesizer) NEVER write to PR state,
  NEVER call `gh pr comment`, NEVER call `gh pr edit --add-label`.
  They return JSON. The orchestrator is the sole writer.
- ASCII-only across every byte the orchestrator writes.
- Never invent new top-level template sections or drop existing
  ones.

## Loop budget

- **Editorial-patch mode:** at most 2 panel rounds. If round 2 still
  carries new_blocking_findings, the synthesizer emits
  `ship_decision: next_brief` with a `ship_prose` note that the
  loop budget is exhausted and the maintainer should ESCALATE
  (manually decide between drafting a fix or closing the PR).
- **New-version mode:** at most 3 panel rounds. Same exhaustion
  semantics on round 3.
- **Editorial-only mode:** zero panel rounds (linter only).

The orchestrator increments and tracks the round counter; subagents
receive it as input but MUST NOT trust panel-side memory.

## Gotchas

- **Roster invariant.** The frontmatter description, the activation
  scope list, the roster table, the topology diagram, and the
  schema enum MUST agree on the 4 panelists + 1 synthesizer. If you
  change one, change all in the same edit.
- **Blocker veto trumps ship-meter.** A panelist who returns one
  `new_blocking_finding` and a shocked_meter of 9 means "the spec is
  mostly excellent but this one thing would break a conformant
  implementation". That blocker still vetoes `fold_and_ship`. Do not
  let the synthesizer average it away.
- **Calibrated severity discipline.** The advisory regime relies on
  panelists distinguishing blocking from recommended honestly. If a
  panelist marks every editorial nit as blocking, the synthesizer's
  blocker veto becomes a denial-of-ship. The panelist prompts state
  the contract explicitly; the synthesizer arbitration prose is the
  safety valve.
- **Wave 0 editorial-only is a noise filter, not a quality
  shortcut.** It exists so a one-line typo fix to a fixture comment
  does not summon four expert agents. It MUST NOT fire on schema
  changes, anchor additions, or fixture-tree topology changes; the
  rules above are intentionally conservative.
- **No-vendor-foundation tokens may appear in panelist returns** (a
  panelist can name "the W3C TAG" as their pedigree in their
  `summary` field) but MUST NOT appear in the synthesizer's
  `fold_now[].patch_instruction` or in the rendered comment body
  outside `<details>` collapsed sections. The synthesizer auto-rejects
  panelist proposals that would add banned tokens to the artifact.
- **ASCII enforcement is per-byte, not per-codepoint.** A character
  with codepoint > 0x7E is non-ASCII even if it would round-trip
  through a different encoding. The linter checks raw byte values,
  not the rendered visual.
- **Subagent write enforcement is contract-based, not sandbox-based.**
  Tool permissions are workflow-scoped, not subagent-scoped, so
  every spawned task technically inherits the same `gh` toolset.
  The "subagents must not write" rule is enforced by the prompt
  contract in each `.agent.md` plus the `safe-outputs.add-comment.max:
  1` fail-soft.
- **Spec drift across count sites.** Linter check 6 catches when sec.
  1.3 sentence, Appendix C trailer, and Appendix D revision-history
  disagree on the normative-statement total. This is the most common
  failure mode of a fold pass and is the reason the linter is
  mandatory before render.

## Relationship to apm-review-panel

`apm-review-panel` is the general OSS multi-persona review for any
non-trivial PR in the repo. `apm-spec-guardian` is its narrow,
spec-only sibling: a different persona roster, a different ship
decision schema (shocked_meter instead of stance enum), and a
mandatory linter step. The architectural shape (FAN-OUT +
SYNTHESIZER + single-writer interlock + advisory regime) is
deliberately the same so a contributor reading one can read the
other. Do not merge them; the persona pedigrees and the artifact
type (spec vs code) are different enough that one-size-fits-all
prompts would dilute both.
