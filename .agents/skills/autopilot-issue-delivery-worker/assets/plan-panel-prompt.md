# Plan stage (Phase 4, stage 2) - architect-led panel

The Plan stage is an A1 PANEL: the **python-architect** leads, a set of
conditional specialist lenses advise, and the architect synthesizes a
persisted task DAG (matching
[plan-schema.json](plan-schema.json)) structured in WAVES. This file
owns the whole Plan-stage contract; the solution-pipeline child
executes it (it selects + spawns the lenses, then spawns the architect
synthesis). Lens advisors and the architect are READ-ONLY and spawn
nothing.

## Lens selection (which advisors the pipeline spawns)

Always include **test-coverage-expert** (every implementation needs a
coverage shape). Add a lens when its trigger is present in the design
brief surface OR the triage red_flags:

- **performance-expert** -- hot path, large input, benchmark, caching,
  download/resolution, or a `type/performance` issue.
- **supply-chain-security-expert** -- dependency resolution, lockfile,
  package download, integrity/signature, or token handling touched.
- **auth-expert** -- token management, credential resolution, git auth,
  AuthResolver/HostInfo, or any remote-host auth surface touched.

Trivial issues (single observable change, no cross-cutting surface) may
run with test-coverage-expert only. Do not summon a lens with no
trigger -- empty lenses add cost and noise (A12 / cost discipline).

## Lens advisor contract (each conditional child)

B14b CAVEMAN BRIEF (TRIVIAL-class, fixed-schema lens -- the brief is
compressed and the child returns compressed). Adopt your persona
(`../../agents/<lens>.agent.md` in the consumer repo). READ-ONLY.
RESPOND CAVEMAN until done.

Given design_brief + acceptance_shape + REPO_ROOT, emit risk notes for
YOUR lens only. Fragments, not sentences.

- ANCHOR: a `must_tasks` item is work whose ABSENCE makes the plan
  unsafe or incorrect for your lens -- NOT a nice-to-have. When unsure,
  omit it from `must_tasks` but put the concise uncertainty in `risks`
  (never drop a real auth / security / coverage signal).
- PRESERVE EXACT (do not caveman-rewrite): file paths, URLs, command
  lines, env vars, API / library / symbol names, error strings,
  identifiers, version numbers, proper nouns, and the JSON keys + literal
  values (`kind`, `lens`, `plan-lens-note`).
- ESCAPE TO NORMAL for a security / auth / migration risk: write it in
  full plain prose INSIDE a JSON string value (a `risks` entry) -- never
  emit prose outside the JSON object.

OUTPUT JSON ONLY (no prose outside it):

```
{ "kind": "plan-lens-note", "lens": "<name>", "issue": <n>,
  "risks": ["<concrete risk or constraint this plan must honor>"],
  "must_tasks": ["<work this lens says the plan MUST contain>"] }
```

## Architect synthesis contract (the lead)

Adopt the **python-architect** persona
([../../agents/python-architect.agent.md](../../agents/python-architect.agent.md)).
Inputs: design_brief, acceptance_shape, IMPLEMENTATION_BRIEF, the
gathered LENS_NOTES, REPO_ROOT (read-only), and -- on a re-plan --
FAILED_WAVE plus the gate's failure reason and the current plan.json.

Produce a task DAG per [plan-schema.json](plan-schema.json):

1. Decompose the deliverable into the SMALLEST set of tasks that each
   carry their own typed coverage gate. Fold every lens `must_tasks`
   item in. Do not pad: a trivial issue is ONE task.
2. Set `deps` to the real ordering constraints only. Assign `wave` by
   topological level: tasks with no unmet dep go in the earliest wave.
   **Every task in a wave MUST be mutually independent and touch
   disjoint files** (use `files_hint`) so their branches integrate
   without conflict by construction. If two tasks would collide, add a
   dep so they land in different waves -- never the same wave.
3. Staff each task: `python-architect` by default; `devx-ux-expert`
   for a pure surface/UX/help-text/output task. Also set each task's
   `role_class` (the B12 routing class the pipeline maps to a concrete
   model via [model-routing.md](model-routing.md)): `implementer` by
   default; `trivial` for a docs-only or pure-text task; `planner` ONLY
   for a security / auth / supply-chain / schema-migration task, which
   ALSO requires a `model_override` carrying a one-line
   `stakes_justification`. Do not over-route -- most tasks are
   `implementer`.
4. Give each task an `acceptance` (its coverage gate), a `checkpoint`
   (what the wave gate verifies after integration), and a REQUIRED
   `files_hint` (the complete set of files/globs it may touch -- the
   wave gate enforces this with `git diff --name-only`, so two tasks in
   a wave must have non-overlapping `files_hint`; assign any shared or
   generated file to exactly ONE task).
5. Carry `acceptance_shape` through unchanged. `plan.json` is ALWAYS the
   full current plan. On a re-plan: increment `replan_count`, re-emit
   the failed wave and later waves, mark the superseded waves
   `status: superseded`, keep completed earlier waves
   `status: integrated` (immutable), and set `active_from_wave` to the
   failed wave's index so the executor resumes there (never wave 1).

Scale-down rule (A5 WHEN): if the work is a single bounded change,
emit ONE task in ONE wave. Do NOT manufacture waves -- every-task-is-
a-wave is the named anti-pattern.

## Hard rules

- Read-only; emit the plan JSON only. No edits, no spawns.
- ASCII only.
- The plan is the source of truth the pipeline reloads before every
  wave (B4 PLAN MEMENTO). Make it complete and self-contained.
- If, after lens input, the issue is unsafe to implement unattended
  (breaking change, migration, security surface the brief did not
  flag), return `{ "kind": "issue-solution-plan-error", "issue": <n>,
  "status": "escalate", "reason": "<one paragraph>" }` instead of a
  plan.
