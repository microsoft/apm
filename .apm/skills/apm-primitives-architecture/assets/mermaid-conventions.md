# Mermaid Conventions for Agentic Module Design

Load this file at design-process step 2-3. It defines which mermaid
diagram types the architect emits at which step, and the
GitHub-render gotchas to avoid.

## Diagram-per-step mapping

| Step | Diagram type | Purpose |
|---|---|---|
| 2 | `flowchart` | component composition: which modules, which depend on which |
| 3 | `sequenceDiagram` | thread spawn / fan-in / interlock points |
| 3.5 | `flowchart LR` | dependency graph: this module + external modules + closure edges |
| optional | `classDiagram` | only when modeling true type hierarchies (rare for primitive design) |

Keep each diagram under 25 nodes. Larger diagrams indicate the
design is a god-module; split it.

## Conventions

### Component diagram (step 2, flowchart)

- Node label = primitive name; do NOT include file extensions in
  labels (extensions are harness-specific affordances).
- Use a node-shape convention to mark module type:

```
flowchart LR
    P((PERSONA))
    S[SKILL]
    R[/RULE/]
    O{ORCHESTRATOR}
    A[(ASSET)]
```

- Edges = `depends-on` (link) relationships, NOT call sequences.
- Mark new vs existing modules:
  - existing: default style.
  - new: `:::new` class with a single class definition at the end:

```
classDef new stroke-dasharray: 5 5;
class NewModule new;
```

### Sequence diagram (step 3, sequenceDiagram)

- Each `participant` = one thread (orchestrator, parent, child A,
  child B, ...).
- Use `->>` for spawn, `-->>` for fan-in / return.
- Annotate interlocks with a `Note over` block.

```
sequenceDiagram
    participant Parent
    participant ChildA
    participant ChildB
    Parent->>ChildA: spawn (lens A)
    Parent->>ChildB: spawn (lens B)
    ChildA-->>Parent: findings
    ChildB-->>Parent: findings
    Note over Parent: synthesize; single-writer interlock on output
```

### Dependency graph diagram (step 3.5, flowchart LR)

- One node per module in scope plus one node per declared external
  module dependency.
- Edge labels mark composition mode: `INLINE`, `LOCAL SIBLING`,
  `EXTERNAL`.
- Show transitive closure edges only when you can name them
  deterministically; otherwise mark `(closure: ...)` as a comment.
- Do NOT include manifest filenames or CLI commands; this diagram
  is at the substrate layer.

```
flowchart LR
    Self[your design]
    Sib[local sibling primitive]
    Ext[(owner/foo)]
    ExtClosure[(owner/foo's deps...)]
    Self -- INLINE --> Self
    Self -- LOCAL SIBLING --> Sib
    Self -- EXTERNAL --> Ext
    Ext -. transitive .-> ExtClosure
```

## GitHub-render gotchas (drift-known)

- `classDiagram` does NOT support inline `:::cssClass` shorthand on
  relationship lines. Use standalone `class Name:::cssClass` lines
  only. Inline form parses on Mermaid Live but fails on GitHub.
- Avoid Unicode arrows (e.g. fancy dashes); use ASCII `-->`,
  `->>`, `-->>`.
- Quote any node label containing `:` or parentheses.
- Subgraphs with the same label across multiple diagrams in one
  file occasionally collapse on GitHub; use unique subgraph IDs.

## What the diagrams MUST and MUST NOT show

MUST show:
- Every primitive module the design depends on.
- Every spawn / fan-in / interlock.
- Whether each module is new or existing.

MUST NOT show:
- Specific file paths or extensions (harness-specific).
- Specific spawn-tool names (harness-specific).
- Internal procedure steps inside one module (those belong in
  the module's natural-language body, drafted later).

## Output discipline

Each diagram block in the handoff packet sits between fenced
``` ```mermaid ``` ``` markers. The handoff packet is markdown;
diagrams are the load-bearing artifacts.
