# Runtime Affordances - Common Substrate

This file defines the converging substrate of agentic primitive
affordances across modern agent harnesses. It is harness-agnostic:
every concept here has an equivalent in every supported harness,
even if the file name, folder location, or trigger field differs.

The architect persona designs against THIS file. Per-harness
adapters in `per-harness/` map the substrate to specific syntax;
load them only when a primitive must reach beyond the substrate.

## The six primitive concepts

### 1. PERSONA SCOPING FILE

A markdown file (with frontmatter) loaded as text into a thread at
the start of execution to bias inference toward a specific lens or
role. It does NOT execute. It does NOT spawn anything. It is a
prompt-shaped knowledge artifact.

Substrate fields (every harness offers these or equivalents):
- a unique `name`
- a `description` (plain text the harness shows to the user / model)
- the body: instructions, principles, anti-patterns

Substrate behavior:
- Loaded by the harness when explicitly invoked or composed.
- Shapes the lens; does not gate tool access (that is the rule
  file's job in some harnesses, or part of the persona body in
  others).

### 2. MODULE ENTRYPOINT (SKILL)

A directory containing a markdown entrypoint file plus an `assets/`
subtree. The entrypoint is loaded into the thread when the harness
matches the module's activation criteria (description-driven
selection, explicit invocation, or both). Assets load lazily on
demand from inside the entrypoint.

Industry standard: agentskills.io defines the SKILL.md +
description-driven activation contract that every modern harness
implements (sometimes with renames or additional fields).

Substrate fields:
- `name`
- `description` (used for activation matching)
- the body: the entrypoint procedure
- `assets/`: arbitrary files (markdown, scripts, images) the
  entrypoint may load at specific steps

Substrate behavior:
- The harness selects which skills to make available based on the
  description matching the user task.
- Once activated, the SKILL.md body loads into the thread; the
  thread reads it and follows its procedure, loading assets only
  at the steps that need them.

### 3. SCOPE-ATTACHED RULE FILE

A markdown file whose content the harness automatically loads into
any thread whose work matches a declared scope. Scope is typically
a glob pattern over file paths or a context predicate.

Substrate fields:
- a scope predicate (glob, path, or other classifier)
- the body: rules, conventions, hard constraints

Substrate behavior:
- Loaded automatically by the harness when the thread's work
  matches the scope.
- The thread does not have to know the rule file exists.

### 4. CHILD-THREAD SPAWN

A built-in capability of any modern agent harness: the running
thread can spawn a CHILD THREAD with its own fresh context window,
optionally seeded with a persona and a task description. The child
returns a value (its final response or a structured result). The
parent is suspended at the spawn site until the child returns
(unless the harness offers async spawn).

Substrate semantics:
- Child has NO access to parent's context except what the parent
  passes as the task description.
- Child MAY load its own personas, skills, rules.
- Parent receives the child's return value as text (or structured
  data, harness-dependent).
- Multiple spawns from the same parent run in parallel where the
  harness supports it.

### 5. TRIGGER ORCHESTRATOR

A scheduled or event-triggered configuration that spawns a session
in response to an external event (timer, repository event, webhook,
user invocation). Lives outside any single session; it is the entry
point that creates sessions in the first place.

Substrate fields:
- a trigger declaration (event, schedule, or interactive)
- a session bootstrap (initial task, initial persona / skill set)
- output channel (where the session's results go)

Substrate behavior:
- Each trigger creates a NEW session. Sessions are stateless across
  triggers unless persistence is engineered explicitly (pattern
  P6 in architecture-patterns.md).

### 6. PLAN PERSISTENCE

Underlying property: ATTENTION DEGRADATION. Tokens far from the
current focus point exert weaker influence on inference. As a
session grows, earlier decisions, todos, and constraints fade from
the model's effective recall even though they remain technically
in-context.

Coping mechanism every modern harness now exposes: a runtime store
where the thread persists its plan and progress, then re-reads the
store at re-grounding boundaries (new step, new file, new spawn).

Substrate fields (every harness offers these or equivalents):
- a PLAN ARTIFACT slot (free-form, typically markdown) capturing
  the problem statement, approach, and step list
- a TODO/STATUS slot (structured, queryable) for per-step status
  with optional dependencies between items
- (optional) a CHECKPOINT slot for milestone snapshots
- (optional) a FILES slot for cross-step artifacts that must
  outlive any single step's context

Substrate behavior:
- The thread WRITES the plan once, EARLY (before drafting modules
  or spawning workers).
- The thread READS the plan again at re-grounding boundaries:
  start of each step, return from a spawn, after a tool failure,
  or whenever uncertainty rises.
- Spawned child threads receive a POINTER to the relevant slice
  of the plan (or a copy of it) in their task description; they
  do not inherit the parent's context.
- The store survives the context window by definition; it is
  the explicit cure for ATTENTION DEGRADATION.

When to use:
- Work spans more than ~3 dependent steps.
- Work spans more than one file.
- Work will spawn one or more child threads that must coordinate.
- Session is expected to be long enough that early constraints
  risk decay.

Skip when:
- Single-shot one-step work where the entire instruction set fits
  comfortably in the prompt and no spawn is involved.

## Substrate invariants

These hold across every supported harness:

- Personas, skills, and rules are TEXT loaded into context. They
  do not execute. They steer inference.
- The thread executes; it spawns; it returns.
- A child thread is the only mechanism for parallelism and
  context isolation.
- No primitive can mutate another primitive's content at runtime.
- Tokens cost attention. Smaller substrates yield sharper
  inference.
- Attention decays with distance from the focus point. State that
  must survive that decay belongs in a persistence slot, not in
  the prompt.

## What the substrate deliberately does NOT cover

The following vary per harness and live ONLY in per-harness
adapter files:

- The actual file extension and folder path for each primitive.
- The exact frontmatter field names (e.g. trigger field syntax).
- The name of the spawn primitive (a tool name, an SDK call, etc.).
- The name of any harness-specific tool that primitives commonly
  use (file ops, web fetch, shell).
- The mechanism for declaring multi-target compatibility (some
  harnesses use a magic folder; others use a config file).
- The exact plan-persistence layout (file path, in-context list,
  embedded SQL, checkpoints folder, etc.). Substrate guarantees
  the SLOTS exist; per-harness adapters name the syntax.

A primitive that stays within this substrate is portable across
all harnesses APM supports. A primitive that reaches into a
per-harness adapter file is intentionally non-portable; that
choice MUST be declared in the primitive's design (see
`portability-rules.md`).

## How to use this file

- Architect persona reasons against THIS file alone.
- The design discipline (skill SKILL.md) loads ONLY this file at
  step 7a (portability check).
- A per-harness adapter file is loaded at step 7b only when 7a
  flagged a per-harness need.

## Index of per-harness adapters

See `per-harness/` for the harness-specific mappings. Each adapter
is structured to map back to the six concepts above, in order.
