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
