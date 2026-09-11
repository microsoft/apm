---
name: apm-triage-panel
description: >-
  Use this skill to triage one microsoft/apm issue selected by the
  daily sweep, an advisory re-triage request, or manual dispatch, even
  when the request only asks whether one proposal is ready for discussion.
  Return one advisory recommendation and a proposed scope brief, never
  human approval. Do not implement the issue or manage the backlog.
---

# APM Triage Panel -- Single-Issue Triage Orchestration

**Advisory only.** Read `assets/label-contract.json` before reasoning.
The caller supplies the repository's GOVERNANCE.md and CONTRIBUTING.md:
those human policies override persona instructions. If unavailable, do
not invent authority or review contacts; flag the missing context.
An `accept` recommendation is not acceptance. Labels and silence are not
approval. Only a responsible human maintainer approves scope, priority,
contributor invitations, review capacity, and release targeting.

The panel is fixed at **2 mandatory specialist lenses + up to 3
conditional lenses + 1 always-active arbiter = 6 persona sections in
one triage comment**. You play each
lens in turn from inside a single agent loop (progressive-disclosure
skill model -- no sub-agent dispatch). Routing chooses *which* lenses
execute; it never changes which headings appear in the final comment.

This skill mirrors the `apm-review-panel` orchestration shape on
purpose. Same single-comment discipline, same completeness gate, same
persona-pass procedure -- only the personas, the rubric, and the
output template differ.

## Agent roster

| Agent | Persona | Always active? |
|-------|---------|----------------|
| [DevX UX Expert](../../agents/devx-ux-expert.agent.md) | User-Need Reviewer | Yes |
| [Supply Chain Security Expert](../../agents/supply-chain-security-expert.agent.md) | Risk-Surface Reviewer | Yes |
| [APM CEO](../../agents/apm-ceo.agent.md) | Triage Arbiter | Yes (always arbitrates) |
| [OSS Growth Hacker](../../agents/oss-growth-hacker.agent.md) | Contributor-Tone Reviewer | Conditional (see below) |
| [Python Architect](../../agents/python-architect.agent.md) | Architecture Reviewer | Conditional (see below) |
| [Doc Writer](../../agents/doc-writer.agent.md) | Documentation Reviewer | Conditional (see below) |

Skipped by default: CLI Logging Expert, Auth Expert. Triage operates
on issue intent, not on diffs -- those personas are invoked downstream
by `apm-review-panel` once a PR exists.

## Routing topology

```
   devx-ux-expert      supply-chain-security-expert
        \_______________________/
                    |
                    |   <-- python-architect (conditional; design /
                    |       architecture / new primitive / new schema)
                    |
                    |   <-- doc-writer (conditional; docs work or
                    |       user-facing change that needs new doc pages)
                    v
                apm-ceo               <----  oss-growth-hacker
           (final call / arbiter)           (conditional; tunes tone
                                             when author is new)
```

- **Specialists raise findings independently** -- no implicit consensus.
- **CEO synthesizes advice** on classification, readiness, and reply tone.
  The persona is not a project officer and cannot ratify human decisions.
- **Growth Hacker, Python Architect, and Doc Writer are side-channels**
  to the CEO when activated. They never block a specialist finding;
  they feed the CEO's arbitration:
  - Growth Hacker tunes the comment's tone for first-time and
    low-interaction contributors.
  - Python Architect flags feasibility and cross-cutting impact, and
    pushes the decision toward `status/needs-design` when warranted.
  - Doc Writer flags whether docs work is implied and whether the
    suggested comment wording is grounded in the user vocabulary used
    in the README and guides.

## Conditional panelists

Three personas are conditional: OSS Growth Hacker, Python Architect,
and Doc Writer. Each follows the same shape: an explicit YES/NO
activation rule plus an inactive-reason fallback. Maximum lenses in a
single triage = 6 (2 mandatory + 3 conditional + 1 arbiter).

### OSS Growth Hacker

Activate `oss-growth-hacker` if either rule below matches.

1. **Fast-path author trigger.** Activate the Growth Hacker lens
   immediately when the issue's author meets ANY of:
   - GitHub `author_association` is `FIRST_TIME_CONTRIBUTOR`,
     `FIRST_TIMER`, or `NONE` against `microsoft/apm`.
   - Author has fewer than 3 prior interactions (issues + PRs +
     comments) on `microsoft/apm`.
   - Issue body explicitly says "first issue", "new to APM", or
     similar.

2. **Fallback self-check.** If author signals are ambiguous, answer
   this before activating the lens:

   > Would the warmth, framing, or pointer-set in the reply meaningfully
   > change if I knew this was someone's first interaction with the
   > project? Answer YES or NO with one sentence.
   > If unsure, answer YES.

Routing rule:

- **YES** -> take the OSS Growth Hacker lens (per the Persona pass
  procedure) and capture its tone-tuning findings.
- **NO**  -> record `OSS Growth Hacker inactive reason: <one sentence>`
  in working notes; do not take the lens.

### Python Architect

Activate `python-architect` if either rule below matches.

1. **Fast-path label / scope trigger.** Activate the Architecture
   Reviewer lens immediately when ANY of:
   - The issue carries `type/architecture` (current or proposed) or
     the `breaking-change` preserved label.
   - The issue body proposes a new top-level CLI command, or a schema
     change to `apm.yml`, `apm.lock.yaml`, or `apm-policy.yml`.
   - The issue body contains keywords indicating cross-module or
     cross-file work, a new module, a new pattern, a new contract, or
     a new primitive design -- e.g. "refactor", "rearchitect", "new
     module", "design", "abstraction", "schema change", "pluggable",
     "introduce X pattern".

2. **Fallback self-check.** If the issue is ambiguous, answer this
   before activating the lens:

   > Does this issue, if accepted as written, require a cross-cutting
   > design decision (interface, data model, migration boundary, or
   > new primitive) before code can land safely? Answer YES or NO
   > with one sentence. If unsure, answer YES.

Routing rule:

- **YES** -> take the Python Architect lens. Capture: feasibility of
  the design as proposed, callouts of cross-cutting impact, and
  whether the issue should land as `status/needs-design` instead of
  `status/accepted`.
- **NO**  -> record `Python Architect inactive reason: <one sentence>`
  in working notes; do not take the lens.

### Doc Writer

Activate `doc-writer` if either rule below matches.

1. **Fast-path label / scope trigger.** Activate the Documentation
   Reviewer lens immediately when ANY of:
   - The issue is `type/docs` or carries `area/docs-site` (current or
     proposed).
   - The issue body proposes documentation, README, reference, guide,
     or migration-note changes.
   - The issue is a user-facing feature that will require new doc
     pages -- e.g. a new CLI flag, a new primitive, a new authoring
     concept.

2. **Fallback self-check.** If the issue is ambiguous, answer this
   before activating the lens:

   > Will an implementing PR for this issue need to add or change
   > user-facing documentation in `docs/src/content/docs/` or in the
   > README? Answer YES or NO with one sentence. If unsure, answer
   > YES.

Routing rule:

- **YES** -> take the Doc Writer lens. Capture: whether docs work is
  implied (and whether `area/docs-site` should be added as a
  secondary `area/*` so the implementing PR is reminded), and whether
  the proposed comment wording is clear and grounded in the user
  vocabulary used in the README and guides.
- **NO**  -> record `Doc Writer inactive reason: <one sentence>` in
  working notes; do not take the lens.

## Triage recommendation rubric

The CEO lens recommends exactly ONE outcome from this rubric:

- `accept` -- direction appears clear and aligned. Propose bounded scope
  for a responsible maintainer to approve; do not invite implementation.
- `needs-design` -- direction is sound but the design must be settled
  before code lands. Recommend design discussion and name in the
  comment exactly what must be designed (interface, data model,
  migration, security boundary).
- `decline-with-reason` -- out of scope for APM as positioned by the
  README spine. Suggest an alternative tool, a workaround, or the
  upstream project. Always courteous, always concrete.
- `duplicate-of #N` -- propose the canonical issue. The orchestrator
  must verify the link resolves before posting.
- `defer-later` -- not ready to invite work, including absent review
  capacity. Recommend deferral (`status/deferred` if a human chooses).
  NEVER map `defer-later` to `status/accepted`. A missing milestone alone
  does not prevent acceptance: scope approval and release targeting differ.
- `auto-handle` -- automated noise such as a daily CLI-consistency
  report PR or scheduled bot issue. Propose closing if the report has
  zero unaddressed High findings; otherwise propose splitting into
  individual issues with the right `area/*` labels and reference back
  to the parent.

## Classification and proposed brief

`assets/label-contract.json` is the canonical label contract, including
human decision states, bot processing, legacy read aliases, and safe
rollout. Do not duplicate or expand its allowlist. Contributors need
not supply a five-axis taxonomy. Propose only useful type/area/theme
classification; null or an empty list is valid when uncertain. At most
six classification labels, with at most one type and one primary theme.

Preserve existing labels, priority, invitations, milestone, and human
approval records. A conflicting classification belongs in advice, not a
replacement. Legacy aliases are read-only compatibility, not permission
to relabel anything. The workflow owns processing markers independently
of the recommendation; this skill never chooses a human status to apply.

Every recommendation includes a concise **proposed**, not approved, brief:
`scope`, `done_when`, `exclusions`, and `review_needs`. State missing
information explicitly. Identify needed expertise and unresolved review
capacity, not an invented assignment or promise. Use the human roster:
core maintainers overlap project-wide; registry public API work goes to
its primary maintainer with core backup, not unrelated registry internals.
Do not require the lead to reapprove every routine decision.

No release milestone, priority, or contributor invitation is emitted as a
machine-actionable field. These remain human decisions.

The caller uses `scripts/triage_state.py` for deterministic, read-only
selection and label planning against the contract. Run it with `--help`
for invocation; it reads normalized JSON from stdin, writes a JSON plan
to stdout, and reports failures on stderr with a nonzero exit. It never
calls GitHub, posts comments, or verifies human approval.

## Quality gates

A triage comment passes when:

- [ ] DevX UX Expert: real user surface identified, the request maps
      (or fails to map) to a concrete README-anchored capability
- [ ] Supply Chain Security Expert: P/G/S risk surfaces assessed; if
      the issue touches lockfile, marketplace, MCP config, signing,
      or auth, name the relevant risk and review expertise
- [ ] APM CEO: recommendation, classification, brief, and tone synthesized;
      human approval is explicitly still required
- [ ] OSS Growth Hacker lens taken or inactive reason recorded; if
      taken, tone tuned for a new or low-interaction contributor and
      the reply names a concrete next step they can take
- [ ] Python Architect lens taken or inactive reason recorded; if
      taken, feasibility, cross-cutting impact, and any
      `status/needs-design` recommendation are captured
- [ ] Doc Writer lens taken or inactive reason recorded; if taken,
      docs implication is named and any `area/docs-site` secondary
      label is proposed when the implementing PR will need new pages

## Notes

- This skill orchestrates a panel **in your own context** -- you are
  the only agent. You load each persona's `.agent.md` reference file
  on demand (progressive disclosure), assume that persona's lens to
  produce its findings, then move to the next persona. Do NOT spawn
  sub-agents (no `task` tool dispatch) -- the panel is a sequence of
  reasoning passes inside one agent loop, not a multi-agent fan-out.
- Persona detail lives in the linked `.agent.md` files. Read each
  one when you switch to that persona; do not pre-load all of them.

## Execution checklist

When this skill is activated for an issue, work through these steps
in order, in a single agent loop. Do not skip ahead and do not emit
any output before the final step.

1. Read the issue context (title, body, labels, author,
   `author_association`, prior comments), supplied human governance,
   and `assets/label-contract.json`. Do not re-fetch issue context
   from inside the skill.
2. Resolve the **three conditional cases** -- OSS Growth Hacker,
   Python Architect, Doc Writer -- using the rules in "Conditional
   panelists" above. For each, record either an activation decision
   or `<Persona> inactive reason: <one sentence>` in working notes.
3. For each mandatory persona (plus any conditional persona that
   activated), follow the **Persona pass procedure** below, one
   persona at a time. Do not try to play multiple personas in a
   single pass.
4. Run the **pre-arbitration completeness gate**:
   - Findings exist in working notes for the 2 mandatory specialists
     (DevX UX Expert, Supply Chain Security Expert).
   - For EACH of OSS Growth Hacker, Python Architect, and Doc Writer:
     exactly one of `<Persona> findings` or `<Persona> inactive
     reason` exists (neither = incomplete; both = inconsistent
     routing).
   - No persona section is missing or empty.
   If any check fails, redo that persona's pass and repeat the gate.
   Do not proceed to step 5 until the gate passes.
5. Take the **APM CEO** lens (load
   `../../agents/apm-ceo.agent.md`) and arbitrate the collected
   findings into a single recommendation, useful classification,
   proposed brief, and reply tone. Still in your own context. CEO
   arbitration may run only after the completeness gate has passed.
6. If the rubric outcome is `duplicate-of #N`, use the caller's
   authenticated GitHub issue-read tool to verify the candidate exists
   and is open before committing the link. If it cannot be verified,
   use `accept` or `needs-design` as appropriate and mention the
   suspected duplicate only in prose.
7. Now (and only now) load `assets/triage-template.md` and fill it
   in with the collected findings, recommendation, classification,
   proposed brief, and suggested comment body.
8. Verify the rendered comment contains every top-level heading from
   the template, all six persona `<details>` sections, and the closing
   `triage-recommendation` JSON block. If any element is missing, re-render
   from the template instead of posting a hand-composed substitute.
9. Return the filled template and its structured `triage-recommendation`
   JSON tail to the caller. The workflow alone posts it via its
   safe-output channel; direct callers receive advice without writes.
   Never post from inside the skill or authorize implementation. This is the ONLY
   output emission for the entire panel run -- no per-persona
   comments, no progress comments.

### Persona pass procedure

For each persona, run this exact procedure in your own context:

1. Open the persona's `.agent.md` file (linked in the roster) and
   read its scope, lens, anti-patterns, and required return shape.
2. From that persona's lens, review the issue title, body, labels,
   author signals, and any prior comments against the scope declared
   in the file.
3. Write the findings to working notes under
   `<persona-name>: <findings>` (or, for an inactive conditional
   persona, `<Persona> inactive reason: <one sentence>`).
4. Drop the persona lens before moving on. Do not emit any comment
   from inside a persona pass; persona findings stay in working
   notes until step 7 synthesizes them.

## Output contract

This contract is non-negotiable -- it is the difference between a
triage that lands as one cohesive comment and one that fragments into
per-persona noise.

- Produce **exactly one** comment per triage run.
- Use `assets/triage-template.md` as the comment body. Keep its
  section headings exactly as written. Adapt the body of each
  section to the issue. Do not invent new top-level sections or drop
  existing ones.
- The trailing fenced ```json block named `triage-recommendation` is
  REQUIRED. Its `schema_version: 2` and `advisory_only: true` distinguish
  it from legacy `triage-decision` comments. Consumers must not interpret
  either format as human approval. No operative status, priority,
  invitation, or milestone fields are present.
- ASCII only inside the comment body and JSON tail. No emojis, no
  Unicode dashes, no box-drawing characters. Use `[+] [!] [x] [i] [*] [>]`
  if status symbols are needed.
- CEO arbitration may run only after the completeness gate passes.
- Never emit findings as separate comments, intermediate progress
  comments, or "I will now invoke X" status comments.
- Load `assets/triage-template.md` **at synthesis time only** (step
  7 above) -- not at activation, not while collecting findings.

## Anti-patterns

- **Over-labelling.** Do not exceed 6 proposed classification labels.
  If you find yourself reaching for 7+, prune the weakest `area/*`.
- **Approval by proxy.** A recommendation, accepted label, agent
  persona, or silence never authorizes implementation or a release.
- **Silent decline.** Do not auto-close or `decline-with-reason`
  without a courteous reason linked to the README spine, the
  manifesto. Every decline names where the
  user can go instead.
- **Vague needs-design.** Never recommend `needs-design` without
  naming, in the suggested comment, exactly what must be designed
  (interface, data model, migration, security boundary). "We need to
  think about this" is not a design-needed reason.
- **Consuming human state.** Keep `status/needs-triage` after advice;
  human decision state and bot processing state are independent.
- **Wildcard heuristics.** Do not activate the OSS Growth Hacker on
  `*new*` or `*first*` keyword matches alone -- always cross-check
  `author_association` and prior interactions on `microsoft/apm`.
  Same discipline for Python Architect (do not fire on the bare word
  "refactor" in unrelated context -- check the issue's actual scope)
  and Doc Writer (do not fire purely on the word "docs" appearing in
  passing -- the issue must propose or imply a doc-surface change).

## Gotchas

- **Roster invariant.** The frontmatter description, the roster
  table, the conditional-panelist rule, the triage template, and the
  quality gates MUST agree on the persona set. If you change one,
  change all of them in the same edit.
- **No new persona required.** This skill deliberately reuses
  `devx-ux-expert`, `supply-chain-security-expert`, `apm-ceo`,
  `oss-growth-hacker`, `python-architect`, and `doc-writer`. Do not
  create a `triage-*` persona; the README spine plus the label
  taxonomy plus the existing CEO arbiter are sufficient grounding.
- **Bundle layout on the runner.** Resolve
  `assets/triage-template.md` relative to the loaded `SKILL.md`.
  Never hard-code an installation directory.
- **No multi-persona-in-one-pass.** Each persona has its own
  `.agent.md` for a reason -- read it when you take that lens, write
  the findings, then drop the lens before moving on.
- **Single-emission discipline is fragile under interruption.** If
  you find yourself wanting to "post a quick partial decision and
  then update it", don't. Buffer in working notes; emit once.
