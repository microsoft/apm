# Architecture Patterns Catalog

Load this file at design-process step 2-3 to pick a topology. Each
pattern has: name, when-to-use, interlock requirement, mermaid
sketch, classic-architecture analogue, durable-truth justification.

Patterns are ordered by frequency of fit, not complexity.

---

## P1. Single-loop sequential

WHEN: one lens, one procedure; OR two lenses with strict sequential
dependency where the second consumes the first's output.

INTERLOCK: none (single thread, single writer).

```
parent thread
   |
   v
   step 1 -> step 2 -> step 3 -> output
```

CLASSIC ANALOGUE: straight-line procedure call.

JUSTIFICATION: avoids spawn overhead when no parallelism exists.

ANTI-PATTERN: do NOT use this when >=3 independent lenses exist
with no shared state. That is fan-out (P2).

---

## P2. Fan-out + parent synthesizer (map-reduce)

WHEN: >=3 independent lenses with no shared state, where each lens
benefits from a fresh context window (isolation from siblings'
findings, full attention on its own domain).

INTERLOCK: parent is the single writer to the final sink. Children
return values; they do not write to shared output.

```
parent thread (orchestrator + synthesizer)
   |
   +-- spawn ---> child thread A (lens A) --+
   +-- spawn ---> child thread B (lens B) --+
   +-- spawn ---> child thread C (lens C) --+
                                            v
                                       fan-in to parent
                                            |
                                            v
                                       synthesize -> single output
```

CLASSIC ANALOGUE: map-reduce; thread pool with join.

DURABLE-TRUTH JUSTIFICATION: each child operates with its own
fresh context window, so its lens's instructions sit at the top of
attention. A single-loop alternative would force all lenses' text
to compete in one window, degrading the later lenses.

VARIATION 2a (conditional fan-out): one child is spawned only if
a routing predicate fires. Predicate evaluated by parent before
spawn.

VARIATION 2b (synthesizer with arbitration): the parent loads a
distinct arbiter persona at the synthesis step (different lens
from the orchestrator role).

---

## P3. Conditional dispatch

WHEN: a single lens, but which procedure runs depends on input
classification.

INTERLOCK: none.

```
parent thread
   |
   classify input
   |
   +-- case A -> procedure A -> output
   +-- case B -> procedure B -> output
   +-- case C -> procedure C -> output
```

CLASSIC ANALOGUE: strategy pattern; switch.

JUSTIFICATION: keeps each procedure's instructions out of context
unless its case matches. Cheaper than fan-out when only one
procedure runs per invocation.

---

## P4. Validation gate

WHEN: a procedure produces an artifact whose correctness can be
checked deterministically before the procedure proceeds.

INTERLOCK: gate is an atomic step; no partial artifact escapes.

```
parent thread
   |
   v
   produce candidate
   |
   v
   gate: validator (deterministic tool / checklist)
   |
   +-- pass -> commit / proceed
   +-- fail -> revise -> back to gate
```

CLASSIC ANALOGUE: precondition assertion; CI gate.

DURABLE-TRUTH JUSTIFICATION: collapses probabilistic output to a
verifiable checkpoint, reducing variance.

---

## P5. Supervisor / worker

WHEN: a long task with checkpointable subtasks, where the
supervisor decides what to spawn next based on prior results
(dynamic plan).

INTERLOCK: supervisor is the single planner. Workers cannot spawn
peer workers (avoids unbounded fan-out).

```
supervisor thread
   |
   v
   plan next step
   |
   +-- spawn worker --> result
                          |
                          v
                       supervisor updates plan -> next step
```

CLASSIC ANALOGUE: actor supervision tree (limited depth).

JUSTIFICATION: lets the supervisor's planning context stay focused
while delegating bounded execution to fresh worker contexts.

---

## P6. Orchestrator + persisted artifact (cross-session)

WHEN: work spans more than one trigger event (e.g. a workflow that
reacts to a PR, then reacts again on a comment, then on merge).
Threads at different times must share state.

INTERLOCK: a persisted artifact (file, label, comment) is the
shared state. Single-writer rule applies per artifact key.

```
trigger 1 ---> session 1 ---> writes artifact ---> exits
                                  |
                  (artifact persists)
                                  v
trigger 2 ---> session 2 ---> reads artifact ---> writes new artifact
```

CLASSIC ANALOGUE: actor with persistent state; event-sourced
workflow.

JUSTIFICATION: durable truth #2 (context must be explicit) applied
across spawn boundaries: the next session starts with no memory
unless the artifact carries it.

---

## P7. Composed module (depend, don't duplicate)

WHEN: a primitive your design needs ALREADY EXISTS as another
module the project can pull in (or could plausibly be one). Most
often: a shared persona, a cross-cutting rule set, a usage skill
for a dependent tool, a domain glossary.

INTERLOCK: not a runtime pattern; a SOURCE-TIME pattern. The
interlock is the dependency edge in the manifest plus version
pinning.

```
your design
   |
   +-- inline asset A             (composition mode: INLINE)
   +-- depends-on local sibling B (composition mode: LOCAL SIBLING)
   +-- depends-on external module
       owner/foo                  (composition mode: EXTERNAL MODULE)
                |
                +-- transitive: owner/foo's own dependency closure
```

CLASSIC ANALOGUE: library import; "depend, don't duplicate";
package-manager-driven composition.

JUSTIFICATION: durable truth #4 (composition is first-class). A
primitive duplicated across N projects drifts; depending on a
single module preserves consistency, allows independent release
cadence, and makes the version explicit via pinning.

PROMOTION RULE: a LOCAL SIBLING that meets any of {rule of three;
independent release cadence; different owner; pinning-worthy}
should be PROMOTED to an EXTERNAL MODULE. See
`composition-substrate.md`.

ANTI-PATTERN: do NOT introduce an EXTERNAL MODULE when none of
the promotion criteria apply -- you trade evolution speed for no
real reuse benefit.

---

## P8. Plan-first with persisted plan

WHEN: any of:
- work spans more than ~3 dependent steps;
- work touches more than one file;
- work will spawn one or more child threads that must coordinate;
- session is expected to be long enough that early constraints
  risk attention decay (architect truth #1).

This pattern is ORTHOGONAL to P1-P7. Combine it with whichever
topology fits the work shape.

INTERLOCK: the executor MUST reload the plan at re-grounding
boundaries (start of each step, return from a spawn, after a tool
failure). Without the reload, the persistence is dead weight.

```
[ planning phase ]            [ persistence layer ]
    decide problem      -->   PLAN ARTIFACT (plan.md or equiv.)
    decompose to steps  -->   TODO/STATUS slot
    pick topology       -->   (CHECKPOINT slot, on milestones)
                                    ^
[ execution phase ]                 |
    step k starts ----- reload -----+
       do work
    step k ends ------- update -----+
       (advance status)
    spawn child? --> child gets POINTER to plan slice in its task
    return from spawn -- reload ----+
       (verify state still matches plan; correct if not)
```

CLASSIC ANALOGUE: write-ahead log + idempotent replay; or
plan-then-execute compilers; or BDD scenario file driving step
implementations.

JUSTIFICATION: cures attention decay (truth #1). Without an
external plan, long sessions silently drop earlier decisions,
todos, and constraints. With one, every re-grounding event is a
chance to recover.

ANTI-PATTERN: writing the plan at the END (post-hoc rationalization)
rather than at the start; or writing it once and never reloading;
or stuffing the entire plan into every spawned child's task
description (defeats context isolation, see P2).

REQUIRED SLOTS (substrate): PLAN ARTIFACT + TODO/STATUS. Optional:
CHECKPOINT, FILES. The runtime-affordances substrate names the
slots; the per-harness adapter names the concrete mechanism.

---

## Selection heuristic (decision flow)

```
   Is the work a single coherent procedure?
            |
       yes / no
       /         \
      /           v
     v          Are there >=3 independent lenses
single-loop    with no shared state?
   (P1)              |
                yes / no
                /         \
               /           v
              /          Is the choice of procedure
             /          determined by input class?
            v               |
   fan-out + synthesizer    yes / no
   (P2)                     /        \
                           /          v
                          /        Is each step verifiable?
                         v             |
                  conditional dispatch  yes / no
                  (P3)                  /        \
                                       /          v
                                      /       Does the work span
                                     v        more than one trigger?
                              validation gate    |
                              (P4)            yes / no
                                              /       \
                                             v         v
                                       orchestrator   supervisor
                                       + artifact     / worker
                                       (P6)           (P5)
```

When two patterns fit, prefer the one that gives each thread a
narrower context (fewer competing tokens).

P8 is orthogonal: combine it with the chosen topology whenever the
WHEN-clause for P8 fires. P8 is the cure for attention decay; the
topology choice (P1-P7) is the cure for parallelism / isolation /
verifiability concerns.
