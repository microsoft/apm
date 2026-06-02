# Model routing (Phase 4 B12 MODEL ROUTER) - authoritative source

Phase 4 spawns a heterogeneous set of children (A12 GRADIENT WORKFLOW):
heavy planning at the front, implementer-class bulk in the middle, cheap
read-only verification at the back. This file is the SINGLE SOURCE OF
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
| Ideate (1/issue)                 | implementer | claude-sonnet-4.6 | down | frames the acceptance_shape contract; moderate stakes |
| Lens advisor (<=4/issue)         | trivial     | claude-haiku-4.5  | down | single-pass advisory checklist, read-only (PR#12 Cell E: lenses at reviewer class = +25% cost, 0 quality delta) |
| Architect synthesis (1/issue +<=2 replans) | planner | claude-opus-4.8 | **UP (stakes)** | produces the task DAG; a wrong plan poisons every wave |
| Task implementer (<=6/wave)      | per task `role_class` | resolved here | mixed | default implementer; docs->trivial; security/migration->planner via `model_override` |
| Wave-gate verifier (2/wave)      | reviewer    | claude-haiku-4.5  | down | grades the candidate diff + the pipeline's deterministic lint/test evidence; ESCALATES (below) |
| Acceptance close (1/issue)       | --          | (pipeline model)  | n/a  | runs INLINE in the pipeline child (sole writer of the issue branch); not a separate spawn, so inherits the pipeline's implementer model |

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
