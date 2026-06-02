# Model routing (Phase 4 B12 MODEL ROUTER) - authoritative source

Phase 4 spawns a heterogeneous set of children (A12 GRADIENT WORKFLOW):
heavy planning at the front, implementer-class bulk in the middle, cheap
read-only verification at the back. The heavy front is deliberately
maximal: the triage gate (Phase 1), Ideate (acceptance_shape contract),
and the architect (task DAG) all bind to opus -- these three stages
determine whether the whole pipeline is correctly scoped, so a wrong call
there poisons everything downstream. This file is the SINGLE SOURCE OF
TRUTH that binds each spawn to a concrete model so the orchestrator does
not have to infer a model from a role-class name (role class alone does
not route -- the spawner needs the concrete SKU).

Binding site: every Phase 4 child is spawned via the orchestrator/
pipeline `task` spawn, which takes a per-spawn `model`. The personas
(`../../agents/<persona>.agent.md`) are SHARED across skills, so the
model is bound at the SPAWN, never pinned in the shared persona file.
On Copilot, SKILL.md frontmatter cannot carry `model:` -- this table is
how Phase 4 routes instead.

## Role class -> concrete model (Copilot SKUs)

Verified: 2026-06-02. Re-verify against the live Copilot models &
pricing page if this stamp is more than 90 days stale:
https://docs.github.com/en/copilot/reference/copilot-billing/models-and-pricing

| Role class  | Concrete model      | Capability profile                          |
|-------------|---------------------|---------------------------------------------|
| trivial     | claude-haiku-4.5    | classify/extract/grade over a finite surface |
| implementer | claude-sonnet-4.6   | reliable coding + tool use, follows a plan   |
| planner     | claude-opus-4.8     | multi-step planning, cross-file reasoning    |

STALE BEHAVIOR: if the stamp above is more than 90 days old, BLOCK the
planner/architect spawn (do not silently downgrade a stakes binding)
and re-verify the SKUs; trivial/reviewer spawns warn-and-continue.

## Per-spawn binding

| Spawn (fan-out)                  | Role class  | Model             | Bind | Why |
|----------------------------------|-------------|-------------------|------|-----|
| solution-pipeline child (1/issue)| implementer | claude-sonnet-4.6 | down | drives git/integration, follows plan.json; no novel planning |
| Ideate (1/issue)                 | planner     | claude-opus-4.8   | **UP (stakes)** | AUTHORS the acceptance_shape contract (B5) that every wave and the acceptance close are verified against -- a wrong contract mis-verifies the whole issue; front-loaded heavy by deliberate design |
| Lens advisor (<=4/issue)         | trivial     | claude-haiku-4.5  | down | single-pass advisory checklist, read-only; fixed-schema, so carries a B14b caveman brief (PR#12 Cell E: lenses at reviewer class = +25% cost, 0 quality delta) |
| Architect synthesis (1/issue +<=2 replans) | planner | claude-opus-4.8 | **UP (stakes)** | produces the task DAG; a wrong plan poisons every wave |
| Task implementer (<=6/wave)      | per task `role_class` | resolved here | mixed | default implementer; docs->trivial; security/migration->planner via `model_override` |
| Wave-gate verifier (2/wave)      | reviewer    | claude-haiku-4.5  | down | grades the candidate diff + the pipeline's deterministic lint/test evidence; fixed-schema, so carries a B14b caveman brief; ESCALATES (below) |
| Acceptance close (1/issue)       | --          | (pipeline model)  | n/a  | runs INLINE in the pipeline child (sole writer of the issue branch); not a separate spawn, so inherits the pipeline's implementer model |

## B14b caveman briefs (layered on the haiku spawns)

B12 picks the model; **B14b CAVEMAN BRIEF** compresses the briefs of the
two cheap, high-fan-out, fixed-schema spawns so their input AND their
returns are token-thin. Genesis gates caveman to `TRIVIAL` or
fixed-schema `REVIEWER` only (open-ended judgement would collapse into
the model's prior), so it applies to exactly two spawns here -- and to no
others:

| Spawn               | Return schema        | Brief lives in            |
|---------------------|----------------------|---------------------------|
| Lens advisor        | `{risks, must_tasks}`| plan-panel-prompt.md      |
| Wave-gate verifiers | `{verdict, failures}`| wave-gate-rubric.md       |

Each caveman brief carries the canonical contract: `RESPOND CAVEMAN until
done` (role-mode persistence, so receipts come back compressed), a single
`ANCHOR` line grounding the highest-risk verdict bucket, a `PRESERVE
EXACT` list (paths / API names / error strings / numbers are never
caveman-rewritten), an `ESCAPE TO NORMAL` clause for security/destructive
findings, and an `OUTPUT JSON ONLY` contract (the JSON receipt schema is
byte-identical to the verbose version). Triage, Ideate, the architect
synthesis, the task implementers, and the pipeline child are NOT caveman
(open-ended or prose-output contracts -- outside the gate).

## PER-SPAWN DECLARATION TABLE (audience boundary, B14c CAVEMAN CHANNEL)

All Phase 4 children are INTERNAL: their receipts feed the pipeline child
and the orchestrator, never the maintainer directly. The pipeline child's
PR body and the Phase 7 final report are the EXTERNAL decompression edge
(normal prose). Genesis gate (audience-boundary): an INTERNAL spawn may
ship a NORMAL, uncompressed brief ONLY when its justification is one of
{security warning, irreversible op, ambiguous multi-step, judgement-
without-schema}; otherwise the brief MUST be caveman. Every NORMAL row
below cites that exception, so each is gate-clean.

| Spawn               | Audience | Tier        | Brief mode   | Receipt mode | Justification |
|---------------------|----------|-------------|--------------|--------------|---------------|
| Triage child        | INTERNAL | PLANNER     | NORMAL       | JSON_RECEIPT | ambiguous multi-step (open triage rubric; grounds against repo state; resolves decision/confidence/red_flags/brief). Paramount front gate -- see Phase 1 binding below |
| Ideate              | INTERNAL | PLANNER     | NORMAL       | JSON_RECEIPT | ambiguous multi-step (authors the acceptance_shape contract every downstream stage is verified against; front-loaded heavy by design) |
| Lens advisor        | INTERNAL | TRIVIAL     | CAVEMAN_FULL | JSON_RECEIPT | fixed schema {risks, must_tasks}; security escape active |
| Architect synthesis | INTERNAL | PLANNER     | NORMAL       | JSON_RECEIPT | ambiguous multi-step (genuine planning; the schema constrains output shape, not the reasoning that builds the DAG) |
| Task implementer    | INTERNAL | IMPLEMENTER | NORMAL       | JSON_RECEIPT | ambiguous multi-step (writes the typed coverage gate + code; returns a status object) |
| Wave-gate verifier  | INTERNAL | REVIEWER    | CAVEMAN_FULL | JSON_RECEIPT | fixed schema {verdict, failures}; security/destructive escape active |
| Acceptance close    | INTERNAL | --          | INLINE       | --           | runs inline in the pipeline child; not a separate spawn |
| Pipeline child      | INTERNAL | IMPLEMENTER | NORMAL       | JSON_RECEIPT | ambiguous multi-step (orchestrates the per-issue pipeline); its EXTERNAL PR body is the prose decompression edge |

Every spawn returns exactly one JSON object, so the receipt mode is
JSON_RECEIPT across the board -- brief mode and receipt mode are
INDEPENDENT axes (a NORMAL brief can still demand a JSON receipt).
NORMAL-prose decompression happens at exactly two EXTERNAL edges: the
pipeline child -> the PR body, and the orchestrator -> the Phase 2 digest
plus the Phase 7 report.

Brief mode is the AUDIENCE-BOUNDARY axis (how compressed the brief is)
and is ORTHOGONAL to role class (which model). Both haiku spawns ship
CAVEMAN_FULL, not CAVEMAN_ULTRA: each carries an ESCAPE-TO-NORMAL clause
plus a multi-field schema, so ULTRA (single-anchor pure classifier) would
strip the escape contract. The pipeline child is the synthesizer that
decompresses INTERNAL JSON receipts into the EXTERNAL PR body (B14c).

B16 EFFORT GOVERNOR is N/A for the currently selected Claude bindings:
Copilot's Claude SKUs fold reasoning effort into the SKU choice (haiku /
sonnet / opus), with no per-call reasoning_effort knob to govern, so the
cost gradient is expressed entirely through B12 SKU selection. If a
GPT-5-class SKU ever replaces any row above, THAT row must declare a
per-call reasoning_effort (B16 goes live for it).

## Routing receipt (parent-auditable B12 audit edge)

The per-spawn JSON_RECEIPT above feeds the pipeline child internally,
but the ORCHESTRATOR cannot see nested routing without reading child
transcripts (the dogfood cost-observability gap: triage=opus and
shepherd=sonnet were confirmable, but nested Ideate/Plan opus-escalation
and haiku CAVEMAN compression were not). Close the gap by bubbling a
machine-readable routing receipt up every return edge:

- Each spawner records, for every child it spawns, a receipt
  `{ spawn, requested_model, role_class, brief_mode, child_echo_model? }`.
  `requested_model` is AUTHORITATIVE (the SKU passed to `task`);
  `child_echo_model` is the child's self-report and is advisory only
  (a child cannot prove the SKU it actually ran under).
- The pipeline child AGGREGATES its descendants' receipts into a
  `routing_receipts` array on its `implement-result` return.
- The shepherd-driver child returns its OWN `routing_receipt` (its model)
  plus `panel_execution` (`skill-tool` | `inline`) and `panel_personas`
  on its `completion_return` (see shepherd-driver completion-schema.json).
- The orchestrator records these on the row so B12/B14b adherence is
  auditable from plan.md alone -- never from a transcript re-read.

Receipts are observability, not a gate: a missing receipt is a soft
signal to inspect, not a hard failure.

## Phase 1 triage binding (the paramount front gate)

Triage runs in Phase 1, not Phase 4, so it is not in the per-spawn table
above; bind it explicitly here. Triage binds to the PLANNER class
(claude-opus-4.8) -- front-loaded heavy by deliberate design. It runs the
OPEN apm-triage-panel rubric as judgement (not the fixed-schema GRADING
the wave-gate reviewer does), grounds every call against the repo at HEAD,
and is the gate that decides whether to spend a WHOLE downstream pipeline
on an issue -- a wrong accept burns a full Ideate/Plan/Implement/shepherd
run. Per A12 you buy quality where stakes are highest, and the front gate
is maximal-stakes (it poisons everything downstream if wrong), so it is
bought at opus alongside Ideate and the architect -- the heavy front of
the gradient. Haiku or sonnet here would be the GRADIENT anti-pattern
(cheap where stakes are highest). Escalate-by-default is the backstop: on
any doubt triage returns `confidence: low` plus `red_flags` and the
orchestrator routes to the human, never to auto-implementation. This is
why the declaration table marks triage PLANNER, not REVIEWER -- it shares
the wave-gate's REVIEWER audience role but sits at the maximal-stakes
front, not the cheap fixed-schema back.

## B13 cache-aware-prefix discipline (the gradient's cache lever)

A12 GRADIENT WORKFLOW composes B12 + B16 + B13 + backbone + B4; B13 is the
largest cost lever and carries no quality tradeoff, so the gradient MUST
honor cache discipline or it trips the GRADIENT WITHOUT CACHE DISCIPLINE
anti-pattern (the implementer middle runs N times). On Copilot the harness
owns the cache breakpoints, so B13 here is INVALIDATOR HYGIENE, not
breakpoint placement:

- ROUTE AT SPAWN START. Every child's model is bound once, at its `task`
  spawn (the per-spawn table above); never switch a model mid-session
  inside one child. A mid-session model switch is a cache invalidator. The
  verifier escalation (haiku -> sonnet) sidesteps THAT invalidator because
  it is a FRESH spawn, not an in-session switch -- but a fresh spawn still
  pays a fresh-spawn prefix cost, so the escalation stays trigger-gated
  (below) rather than running by default.
- STABLE REGION = THE LOADED BODIES. The dominant cacheable prefix is not
  a brief's own header but the bodies each child LOADS: the apm-triage-
  panel rubric, the typed-coverage lens, the adopted persona. Keep THOSE
  byte-stable across spawns that load them. The per-spawn `Inputs` block
  (issue body, candidate diff, task slice) is small and bounded by design,
  so its position is a minor factor -- but do NOT inline a large volatile
  blob (a full diff, a file dump) into a brief header ahead of the rubric;
  reference it by `gh` / `git` or keep it inside the bounded Inputs block.
- NO VOLATILE CONTENT IN THE CACHED REGION. Keep timestamps, run ids, and
  head shas out of the stable brief prefix and the loaded bodies; they
  belong in the bounded Inputs block or come from a `gh` / `git` call.
  (The `Verified:` date stamps in this file are maintenance metadata,
  never part of a spawn-brief prefix.)
- ORCHESTRATOR SUFFIX HYGIENE (parent-level B13 x B4). The A11
  reconciliation loop is long-lived across the batch; if it accumulates
  the full per-issue transcript its context grows until the harness
  COMPACTS -- and a compaction is itself a cache invalidator at the parent
  level. Keep the orchestration instructions fixed at the top, reload only
  bounded plan.md / plan.json slices at each phase boundary (B4 PLAN
  MEMENTO, B8 ATTENTION ANCHOR), and page or summarize finished-issue
  state out of the working window instead of carrying it forward.

## B15 tool-subset status (read-only children: DECLARED-NOT-BOUND)

The read-only children (triage, lens advisor, wave-gate verifiers) use a
tiny tool subset (gh / git read, ruff, file read) yet inherit the harness
full catalogue -- the IMPLICIT FULL SURFACE cost grows as the operator
installs MCP servers. The Copilot binding site for a tool allowlist is
`.agent.md` frontmatter; SKILL.md cannot carry `tools:`, and the personas
these children adopt (python-architect, devx-ux-expert) are SHARED across
skills, so this skill MUST NOT unilaterally narrow them. B15 is therefore
DECLARED-NOT-BOUND, and that gap is an ACCEPTED RESIDUAL cost/risk, not a
fix: the implicit full surface stays billable and reachable. The clean
future binding is a dedicated read-only `.agent.md` persona that carries a
`tools:` allowlist AND that these children actually INVOKE at spawn (not
merely "adopt" in prose) -- only an invoked binding narrows the real
surface. Until that persona exists, the subset is a documented
expectation, tracked as future work, not an enforced frontmatter field.

## Wave-gate verifier escalation (reviewer haiku -> implementer sonnet)

The verifiers default to claude-haiku-4.5, but scope-drift detection over
an integrated diff can need cross-file reasoning. Re-run a verifier at
claude-sonnet-4.6 (do NOT decide on the haiku verdict alone) when ANY
deterministic trigger fired in the wave:

- a task touched files outside its `files_hint`;
- a public API / function signature changed;
- an auth, security, supply-chain, lockfile, or schema-migration surface
  was touched;
- existing test files were rewritten (not merely added to);
- the integrated diff is large (the pipeline's own threshold);
- the two verifiers DISAGREE (one pass, one fail).

Fail closed: if a re-run is required and cannot run, treat the gate as
FAIL and re-plan.

## How the pipeline resolves a model

1. For a FIXED spawn, read its row above -> use the named model.
2. For a TASK implementer, read the task's `role_class` (and
   `model_override` if present) from plan.json -> map via the role-class
   table above. `model_override` MUST carry a `stakes_justification`.
3. Pass the resolved model to the `task` spawn. Never infer a model from
   the role-class name without this table.

## Maintenance guard

The fixed-spawn assets name their concrete SKU inline for the spawner's
convenience. Those inline names MUST match this table. On a SKU refresh:
update this table FIRST, then grep the assets for the OLD SKU string and
update every inline occurrence. The role class is the durable binding;
the SKU is the resolved value.
