---
name: apm-primitives-architecture
description: >-
  Use this skill BEFORE drafting any agentic primitive module (skill,
  persona scoping file, scope-attached rule file, orchestrator workflow)
  or when refactoring an existing one. Activate whenever the task asks
  to design, restructure, or critique an agentic module across
  .apm/skills/*, .apm/agents/*, .apm/instructions/*, or workflow files
  that load them. The skill drives a 7-step disciplined design process
  whose output is mermaid diagrams + an interface sketch + a handoff
  packet that the calling thread (or a coder persona it loads) then
  turns into natural-language modules. Do not skip to natural-language
  drafting before the design artifacts exist.
---

# Agentic Primitives Architecture (design discipline)

[Architect persona](../../agents/apm-primitives-architect.agent.md)

This skill encodes a disciplined process for designing agentic
primitive modules. Markdown that steers an LLM is code; you do not
write production code without a design. The output of this skill is
DESIGN ARTIFACTS, not finished modules. A separate coding step
emits the natural-language modules from the artifacts.

## When to activate

- Authoring a new skill, persona scoping file, scope-attached rule
  file, or orchestrator workflow.
- Refactoring an existing module that violates SoC, composition,
  or threading rules (e.g. sequential single-loop where fan-out
  fits).
- Cross-cutting redesigns spanning multiple primitive modules.
- Reviews where structure (not domain content) is in question.

## Hard rules

- Diagrams are written before any natural-language module body.
- No harness-specific syntax appears in the persona reasoning or in
  this SKILL.md. Harness syntax lives only in
  `assets/runtime-affordances/per-harness/<harness>.md` and is
  loaded only at step 7.
- A primitive that targets multiple harnesses MUST be designed
  against `assets/runtime-affordances/common.md` first; reaching
  into a per-harness adapter requires a justified declaration per
  `assets/runtime-affordances/portability-rules.md`.
- The handoff packet at step 6 is the only artifact passed forward.
  No tacit context.

## Process

```
   1 intent + scope
        v
   2 component diagram   <-- load assets/mermaid-conventions.md
        v                    load assets/architecture-patterns.md
   3 thread / sequence diagram
        v
   4 SoC pass against existing modules
        v
   5 classic + PROSE + LLM-physics compliance check
        v
   6 handoff packet (diagrams + interface sketch + declared targets)
        v                                      [DESIGN ENDS HERE]
   ----- caller / coder thread takes over -----
   7a portability check
        v                  load runtime-affordances/common.md (always)
   7b draft natural-       load runtime-affordances/per-harness/<x>.md
      language module      ONLY if step 7a flagged a per-harness need
        v
   8 validate against diagrams + lint (PROSE 5-axis, size budget,
     ASCII, coherent unit, portability honored)
```

### Step 1 - intent + scope

Write one paragraph: the user-facing capability, the trigger
conditions, the boundary (what it does NOT do). Apply Single
Responsibility: if the paragraph contains "and" connecting two
distinct capabilities, split into two designs.

### Step 2 - component diagram (mermaid)

Load:
- `assets/architecture-patterns.md`
- `assets/mermaid-conventions.md`

Emit a `flowchart` showing every primitive module the design uses
and which other modules it depends on (via links). Mark which
modules already exist vs new. Mark each module with one of:
PERSONA, SKILL, RULE, ORCHESTRATOR, ASSET.

### Step 3 - thread / sequence diagram (mermaid)

Emit a `sequenceDiagram` showing:
- Which thread spawns which (subagent fan-out).
- Where parent waits (fan-in / synthesis).
- Any interlock on shared sinks (one-writer rule).

If the design has >=3 independent lenses with no shared state and
the diagram shows a single-thread loop, redo: it is a fan-out
opportunity. The default for that shape is fan-out + parent
synthesizer.

### Step 4 - SoC pass

For each module in the component diagram, check:
- Does an existing module already do this? If yes, depend on it
  via link; do not duplicate.
- Does this module overlap a sibling's trigger conditions? If yes,
  redraw boundaries.
- Does this module inline content that belongs in a separate
  persona / rule? If yes, extract.

### Step 5 - compliance check

Apply each row of the persona's classic principles table; flag
violations with severity (BLOCKER / HIGH / MEDIUM / LOW). Then
apply the PROSE constraints (Progressive Disclosure, Reduced
Scope, Orchestrated Composition, Safety Boundaries, Explicit
Hierarchy) and the three durable LLM truths. Any BLOCKER stops
the design; return to step 2.

### Step 6 - handoff packet

Produce a single artifact containing:
- The component diagram (step 2).
- The thread/sequence diagram (step 3).
- A short interface sketch per module: name, trigger description,
  inputs, outputs, dependencies (as relative links).
- The declared target set: `common-only` | `<list of harnesses>`.
- Any compliance findings still open (with severity).

DESIGN ENDS HERE. Stop. Do not draft natural language.

### Step 7a - portability check (caller-side)

Caller loads `assets/runtime-affordances/common.md`. For each
module in the handoff packet, check whether its required
affordances are all in the common substrate.

If yes -> declared target = `common-only`; proceed to 7b loading
only `common.md`.

If no -> consult `assets/runtime-affordances/portability-rules.md`.
Either justify reaching into a specific harness adapter (and
declare the constraint in the module header) or redesign to fit
common substrate (return to step 2).

### Step 7b - draft natural-language module(s) (caller-side)

Using the loaded substrate (and per-harness adapter if justified),
emit each module's body. This is the only step that touches
today's syntax.

### Step 8 - validate (caller-side)

- Each emitted module matches its interface sketch in the handoff
  packet.
- Token / line budget honored where the substrate specifies one.
- ASCII only.
- Coherent unit (single responsibility).
- Declared targets honored: no per-harness syntax leaked into a
  `common-only` module.

## Default pattern selection

When in doubt, pick the pattern that minimizes context degradation
in any one thread:

- 1 lens, 1 procedure -> single-loop sequential.
- >=3 independent lenses, no shared state -> fan-out + synthesizer.
- 2 lenses with sequential dependency -> single-loop sequential
  with a validation gate between them.
- Long-running cross-session work -> orchestrator with persisted
  artifact between phases.

See `assets/architecture-patterns.md` for the catalog.

## Worked example

See `assets/worked-example-review-panel.md` for a worked
re-architecture of a real panel from single-loop to fan-out +
parent synthesizer. Use it as the canonical reference shape when
designing any multi-lens module.

## Outputs

A design session produces:

- The handoff packet (section "Step 6") committed alongside the
  module(s) it designs, OR posted as a comment on the PR that
  introduces them.
- The natural-language module bodies (drafted in step 7b).

The handoff packet is the source of truth for any future
refactor: re-running this skill starts from it, not from the
emitted natural language.
