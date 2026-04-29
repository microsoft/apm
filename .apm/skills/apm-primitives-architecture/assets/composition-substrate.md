# Composition Substrate (durable concepts)

Loaded at design step 3.5. Defines the durable mental model for
agentic primitive composition. Names mechanisms; does NOT bind to
any specific module-system tool. Today's tool (APM) is reached only
at the coder step via `assets/module-system-adapters/apm.md`.

If you find yourself writing a manifest filename, a CLI command, a
lockfile field, or a dependency-spec syntax in this layer, stop:
that belongs in the adapter, not here.

---

## The 6 substrate concepts

Each concept is a name plus the design-time question it forces.

### 1. MODULE

A unit of distribution. Bundles one or more primitives + declared
dependencies + identity (name, version, owner). One primitive may
itself be a module.

DESIGN QUESTION: is this primitive a leaf inside its consumer, or
its own module with consumers?

### 2. DEPENDENCY

A directed edge from a module to another module it requires. The
edge carries a target identifier and (typically) a version
constraint.

DESIGN QUESTION: does this design need any dependency edges? On
what? Are they local (same source tree) or external (resolved by
the tool)?

### 3. DISTRIBUTION BOUNDARY

The line separating "ships inside this module" from "consumed from
outside". Crossing the boundary makes the dependency observable to
all consumers and subject to versioning, conflict resolution, and
governance.

DESIGN QUESTION: which boxes in the component diagram cross the
boundary? Inline content stays under the author's control; crossing
the boundary trades control for reuse.

### 4. TRANSITIVE CLOSURE

A module's full dependency graph including dependencies of
dependencies. The closure is what actually loads at runtime, not
just the direct edges.

DESIGN QUESTION: what is the (possibly N-deep) closure of every
external dependency this design adds? Does anything in the closure
duplicate an existing module the project already ships?

### 5. VERSION PINNING

A constraint that fixes the resolved version of a dependency to a
specific identifier (commit, tag, hash). Without pinning, the
closure is non-reproducible.

DESIGN QUESTION: which dependencies need pinning vs floating? Pin
anything whose drift would silently change the runtime behavior of
this module.

### 6. PORTABILITY MODE

Some module systems support an export shape consumed WITHOUT the
tool itself (analogous to how a built artifact is consumed without
the build tool). This unlocks consumers who cannot or will not
adopt the source tool.

DESIGN QUESTION: must this module be consumable by callers who
don't run the module-system tool? If yes, design respects the
exportable shape from the start; if no, free to use authoring-time
features.

---

## Composition modes (the step 3.5 decision)

Every box in the component diagram is one of:

- INLINE: content lives inside this primitive's own file(s). No
  dependency edge is created.
- LOCAL SIBLING: content lives in a sibling primitive in the same
  source tree. Dependency edge but no DISTRIBUTION BOUNDARY
  crossed.
- EXTERNAL MODULE: content lives in another module resolved by the
  module-system tool. Dependency edge crosses the boundary; subject
  to TRANSITIVE CLOSURE, VERSION PINNING, and (if applicable)
  PORTABILITY MODE.

Picking EXTERNAL MODULE requires at least one of:

- RULE OF THREE: same content needed in 3+ projects.
- INDEPENDENT RELEASE CADENCE: the content evolves on its own
  schedule, separate from this consumer.
- DIFFERENT OWNER: the content is governed by a different team /
  org.
- PINNING-WORTHY: drift in this content would silently change
  behavior; explicit pinning is the right control.

If none apply, prefer LOCAL SIBLING or INLINE: cheaper to evolve,
no transitive surface, no governance overhead.

---

## Anti-patterns flagged at this step

- DUPLICATED LEAF: same content inlined in N primitives where one
  EXTERNAL MODULE would do.
- HIDDEN EXTERNAL: a primitive depends on content that is not
  declared as a dependency edge (the runtime "just happens" to
  load it).
- UNPINNED CRITICAL DEP: an external module whose drift would
  silently change behavior, with no pin.
- TRANSITIVE BLOAT: a thin wrapper module pulling in a heavy
  closure when only one piece is needed.
- BOUNDARY VIOLATION: an "internal" sibling that has crept into
  cross-project use without being promoted to a proper module
  (no version, no identity, no owner).
- TOOL LEAK: the architect or skill body naming a specific
  manifest filename / CLI command instead of using these durable
  concepts.

---

## Notes for the coder step (7b)

When emitting natural-language modules, the coder thread maps
substrate concepts to the current module-system tool's syntax via
`assets/module-system-adapters/<tool>.md`. The coder NEVER
re-derives substrate concepts; it only translates them.

This mirrors how runtime affordances work:
substrate (`runtime-affordances/common.md`) +
adapter (`runtime-affordances/per-harness/<x>.md`).
