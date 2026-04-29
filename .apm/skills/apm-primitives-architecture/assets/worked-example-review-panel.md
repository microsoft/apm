# Worked Example: Re-architecting a Multi-Lens Review Panel

Load this file when designing any multi-lens module (panel, audit,
multi-perspective critique). It walks through one real
re-architecture using the design discipline.

## The starting design (anti-pattern)

A "review panel" runs 5 mandatory specialist lenses + 1 conditional
specialist + 1 arbiter, all inside ONE thread. The thread loads each
persona file in turn, plays each lens, accumulates findings in
working notes, then loads the arbiter persona and synthesizes a
single output comment.

### Why it is wrong (durable-truth analysis)

Truth #1: context is finite and fragile.
- By the time the arbiter step runs, the thread's window contains:
  the orchestrator wrapper + 5 personas' text + 5 sets of findings
  + the conditional persona text + the arbiter persona + the
  template. The first persona's text is now far from focus, and
  its lens's nuances have decayed influence on the synthesis.

Truth #2: context must be explicit.
- The lenses cannot operate independently: each lens reads the
  prior lenses' findings whether the design wants that or not,
  because all share the window. That cross-pollination corrupts
  the "independent specialist" contract the design claims.

Truth #3: output is probabilistic.
- The variance is highest exactly where the design demands the
  most precision: the arbiter's synthesis runs at the deepest
  point of attention degradation.

Classic-principle analysis:
- SHARED MUTABLE STATE: all lenses write to the same window.
- CONTEXT THRASH: one thread plays >=5 distinct lenses.
- UNREACHED ESCAPE HATCH: textbook fan-out (>=3 independent
  lenses, no shared state) executed as single-loop.

## The redesigned shape (pattern P2: fan-out + parent synthesizer)

```
flowchart LR
    O{Panel orchestrator}
    P((Architect))
    L((Logging UX))
    U((DevX UX))
    Sec((Security))
    G((Growth))
    Auth((Auth))
    A((Arbiter))
    O --> P
    O --> L
    O --> U
    O --> Sec
    O --> G
    O -. conditional .-> Auth
    O --> A
    classDef new stroke-dasharray: 5 5;
    class A new;
```

Each lens gets its own thread with its own fresh context window.
The parent (orchestrator) is the only writer to the output sink.

```
sequenceDiagram
    participant Orchestrator
    participant LensA as Architect
    participant LensB as LoggingUX
    participant LensC as DevXUX
    participant LensD as Security
    participant LensE as Growth
    participant Auth as Auth (cond)
    participant Arbiter
    Orchestrator->>LensA: spawn(load Architect persona, scope=PR)
    Orchestrator->>LensB: spawn(load LoggingUX persona, scope=PR)
    Orchestrator->>LensC: spawn(load DevXUX persona, scope=PR)
    Orchestrator->>LensD: spawn(load Security persona, scope=PR)
    Orchestrator->>LensE: spawn(load Growth persona, scope=PR)
    alt conditional fires
        Orchestrator->>Auth: spawn(load Auth persona, scope=PR)
        Auth-->>Orchestrator: findings
    end
    LensA-->>Orchestrator: findings
    LensB-->>Orchestrator: findings
    LensC-->>Orchestrator: findings
    LensD-->>Orchestrator: findings
    LensE-->>Orchestrator: findings
    Note over Orchestrator: completeness gate
    Orchestrator->>Arbiter: spawn(load Arbiter persona, plus all findings as input)
    Arbiter-->>Orchestrator: synthesized verdict
    Note over Orchestrator: single-writer interlock on output sink
```

## Why each thread loads only ITS persona

This is the key disambiguation. The Architect THREAD is a runtime
spawn (fresh context window). At startup, that thread loads the
Architect PERSONA (a markdown file used as a scoping prompt). The
persona is not a thread; the thread is not the persona. The thread
exists because we need fresh context; the persona exists because
we need a focused lens. They cooperate.

The arbiter thread is the only one that loads multiple inputs:
it loads the arbiter persona AND the findings from each lens
(passed as text). It does NOT load the lenses' personas; that
would defeat the purpose of having run them in separate threads.

## SoC and dependency check

- Each lens persona = one Single Responsibility (one specialist
  domain).
- The orchestrator skill DEPENDS ON each persona via link; it does
  not inline persona content. Composition over inheritance.
- The arbiter is a distinct persona (not the orchestrator wearing
  a hat). Single Responsibility.

## Compliance summary

| Check | Old design | New design |
|---|---|---|
| Reduced Scope (per-lens fresh window) | FAIL | PASS |
| Orchestrated Composition (independent contracts) | FAIL | PASS |
| Single-writer interlock on output | PASS | PASS |
| God module avoidance | FAIL (one thread = many lenses) | PASS |
| Fan-out where applicable | FAIL | PASS |

## Handoff packet shape (template)

When you produce a real design, output a packet that looks like:

```
## Design: <module name>

### Component diagram
<flowchart>

### Thread / sequence diagram
<sequenceDiagram>

### Interface sketches
- <module>: trigger=<...>, inputs=<...>, outputs=<...>, depends=[<links>]
- ...

### Declared targets
common-only | <list of harnesses (justify each)>

### Open findings
- <severity>: <finding>
```

The coder thread takes this packet and proceeds to step 7
(portability check, then natural-language drafting).
