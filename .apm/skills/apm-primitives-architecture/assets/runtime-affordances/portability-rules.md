# Portability Rules

When a primitive's design needs an affordance NOT covered by the
common substrate (`common.md`), follow these rules. Default bias:
stay within the substrate.

## Decision flow

```
   Step 7a flagged a per-harness need.
         |
         v
   Can the design be redrawn so the need disappears?
         |
   yes / no
   /         \
   v          v
redesign   Is the need a hard requirement of the
(back to   primitive's value (e.g. it MUST run on
step 2)    a schedule, only one harness offers
            scheduled triggers)?
                |
            yes / no
            /         \
            v          v
       declare a    redesign or drop the need
       single
       target;
       depend on
       that
       harness's
       adapter
       file
```

## When a per-harness reach is justified

- The capability has no equivalent in the substrate AND is core to
  the primitive's purpose (e.g. a workflow that must run on
  schedule, where only one harness's orchestrator offers
  scheduling).
- The primitive is intentionally single-target (e.g. an
  installation helper for a specific harness).

## When a per-harness reach is NOT justified

- Convenience: "this harness has a nicer field for this".
- Familiarity: "I know this harness's syntax better".
- Optimization that the substrate could express with one extra
  step.

## Declaration requirement

When a primitive intentionally targets a single harness (or a
subset), the primitive's header MUST state the constraint
explicitly. Examples (substrate-level wording, not harness-level):

```
Targets: <single harness adapter>
Reason: <why the substrate cannot express this>
```

The handoff packet from step 6 must record the same constraint.
This makes portability cost visible in every PR that introduces
or modifies the primitive.

## Multi-target primitives

A primitive may target multiple harnesses simultaneously. In that
case:
- The primitive's body uses substrate-only wording.
- Each per-harness adapter file is responsible for translating the
  substrate concepts into that harness's deployment.
- APM (or the equivalent multi-target deployer) is responsible for
  emitting harness-specific files from the substrate primitive.

A multi-target primitive that contains ANY harness-specific syntax
inline is broken. Move the syntax into the adapter file or the
deployer; never into the primitive.

## Adding a new harness

When a new harness becomes a supported target:

1. Create `per-harness/<harness>.md` mapping each substrate concept
   (the five from `common.md`) to the harness's actual affordances.
2. Cite the harness's official documentation.
3. Note any concepts the harness lacks (and what the workaround
   is, if any).
4. Update no other file. Open-closed: the architect persona, the
   skill, the substrate, and existing primitives are unchanged.

## Removing a harness

When a harness is dropped:

1. Delete `per-harness/<harness>.md`.
2. Audit primitives for declared targets that named the dropped
   harness; redesign or drop those primitives.
3. The substrate, the architect persona, the skill, and other
   harness adapters are unchanged.

## Test of correctness

A primitive passes the portability test when:
- Its body cites only substrate concepts OR
- Its body declares specific targets and cites only the named
  adapters' concepts.

A primitive fails when its body silently mixes substrate vocabulary
with one harness's concrete syntax.
