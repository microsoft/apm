# Wave gate (Phase 4, stage 3) - inter-wave checkpoint (S4)

After the pipeline child integrates a wave's task branches into the
issue branch, it runs THIS checkpoint before advancing to the next
wave. The gate is an S4 VALIDATION DECORATOR staffed by two read-only
verifier children: a **plan-guardian** (python-architect) and an
**ideator** (devx-ux-expert). The pipeline synthesizes their returns
into PASS or FAIL and acts on it.

## When the gate runs (and when it is light)

- Multi-task / multi-wave plan: run the full gate after EVERY wave.
- Trivial single-task, single-wave plan: skip the two verifier spawns;
  the pipeline still runs the lint contract + the issue's `acceptance`
  checks directly. (Scale-down -- do not stage ceremony for a one-task
  issue.)

## Integration is TRANSACTIONAL (pipeline, before spawning verifiers)

Integration must never leave the issue branch in a poisoned or
half-merged state. Use a disposable candidate branch:

1. Record `wave_base_sha = git rev-parse HEAD` on the issue branch
   BEFORE touching anything. Persist it on the wave row in plan.json
   (`base_sha`).
2. Pre-merge disjointness check (deterministic, no merge yet): for each
   task branch, `git diff --name-only <wave_base_sha>..<task-branch>`.
   If any task touched a file outside its `files_hint`, OR two task
   branches in the wave touched the same file, that is a PLANNING error
   -> record it as the FAIL reason and RE-PLAN FROM THIS WAVE (do not
   merge). This catches the conflict before the working tree is dirtied.
3. Create a candidate branch at the base:
   `git branch <issue-branch>-w<wave>-r<replan>-cand <wave_base_sha>`
   and integrate each task branch into the CANDIDATE
   (`git merge --no-ff`), never directly into the issue branch.
4. If a merge still conflicts despite step 2 (e.g. semantic/generated
   overlap), `git merge --abort`, delete the candidate branch, and
   RE-PLAN FROM THIS WAVE with the conflict recorded. The issue branch
   is untouched at `wave_base_sha`.
5. The verifiers (below) run against the CANDIDATE branch. Only on PASS
   does the pipeline fast-forward the issue branch to the candidate
   (`git merge --ff-only <candidate>`) and delete the candidate.

## Verifier children (read-only, spawned in parallel)

Both verifiers are spawned at REVIEWER class -- `claude-haiku-4.5`
first-pass (B12; they grade a concrete candidate diff against the plan
plus the pipeline's deterministic lint/test evidence). They ESCALATE to
`claude-sonnet-4.6` per the rule below; resolve the model via
[model-routing.md](model-routing.md).

### Verifier escalation (haiku -> sonnet)

Before fanning in the verdicts, re-run a verifier at `claude-sonnet-4.6`
(do NOT decide on the haiku verdict alone) when ANY deterministic
trigger fired in this wave:

- a task touched files outside its `files_hint`;
- a public API / function signature changed;
- an auth, security, supply-chain, lockfile, or schema-migration surface
  was touched;
- existing test files were rewritten (not merely added to);
- the integrated diff is large (the pipeline's large-diff threshold);
- the two verifiers DISAGREE (one pass, one fail).

Fail closed: if a required escalation re-run cannot run, treat the gate
as FAIL and re-plan.

### plan-guardian (python-architect)

B14b CAVEMAN BRIEF (fixed-schema REVIEWER -- compressed brief, compressed
return). Adopt python-architect. READ-ONLY. RESPOND CAVEMAN until done.
Inputs: the CANDIDATE branch (checked out in a read-only worktree), this
wave's tasks (with each task's `checkpoint`), and the lint/test evidence.
Verify, read-only:

- Every task in the wave is present and its `checkpoint` holds.
- The full lint contract is silent and the test suite is green on the
  integrated branch.
- No scope drift beyond the plan (no unplanned subsystem, no
  auth/security/migration surface that was not flagged).

- ANCHOR: verdict=fail on ANY scope drift, any unflagged auth / security
  / migration surface, OR any checklist bullet you could not observe true
  (unverified / uncertain = fail). PASS only when every bullet above is
  observed true -- a false pass poisons every later wave.
- FAILURE SHAPE: each `failures` entry names the exact task / `checkpoint`
  / file, the observed miss, and the re-plan action needed. Compress the
  wording, NOT the cause or evidence (the architect re-plans from these).
- PRESERVE EXACT: file paths, URLs, command lines, env vars, `checkpoint`
  text, API names, error strings, numbers, proper nouns, and the JSON
  keys + literal values (`kind`, `role`, `gate-note`, `plan-guardian`,
  `pass`, `fail`).
- ESCAPE TO NORMAL for a security / destructive finding: state it in full
  inside a `failures` JSON string value -- never as prose outside the JSON.

Return JSON ONLY: `{ "kind":"gate-note","role":"plan-guardian","verdict":"pass|fail",
"failures":["<concrete miss>"] }`.

### ideator (devx-ux-expert)

B14b CAVEMAN BRIEF (fixed-schema REVIEWER). Adopt devx-ux-expert.
READ-ONLY. RESPOND CAVEMAN until done. Inputs: the CANDIDATE branch + the
original `acceptance_shape`. Verify, read-only, that the integrated state
still moves toward (and does not contradict) the acceptance_shape from
the user's point of view.

- ANCHOR: verdict=fail if any acceptance_shape condition is now
  contradicted, unreachable, unverified, or uncertain. PASS only when no
  condition regressed.
- FAILURE SHAPE: each `failures` entry names the acceptance_shape
  condition at risk + why, in compressed wording (keep the cause).
- PRESERVE EXACT: acceptance_shape wording, file paths, command lines,
  URLs, env vars, proper nouns, and the JSON keys + literal values
  (`kind`, `role`, `gate-note`, `ideator`, `pass`, `fail`).
- ESCAPE TO NORMAL for a user-data / destructive concern: state it inside
  a `failures` JSON string value -- never as prose outside the JSON.

Return JSON ONLY: `{ "kind":"gate-note","role":"ideator","verdict":"pass|fail",
"failures":["<acceptance_shape condition now at risk>"] }`.

## Synthesis (pipeline)

- BOTH verdicts pass AND lint silent AND tests green -> **PASS**:
  fast-forward the issue branch to the candidate
  (`git merge --ff-only <candidate>`), delete the candidate branch, then
  ALWAYS remove this wave's task worktrees and delete their local task
  branches. Advance to the next wave (or to acceptance close if this was
  the last wave).
- ANY fail, OR an integration conflict, OR red lint/tests -> **FAIL**:
  reset cleanly first -- `git merge --abort` if a merge is in progress,
  delete the candidate branch, and confirm the issue branch is still at
  `wave_base_sha` (the issue branch was never written, so no reset is
  needed; never `reset --hard` the issue branch on a passed-wave base).
  Then RE-PLAN FROM THIS WAVE: re-spawn the Plan architect (plan-panel-
  prompt.md) with FAILED_WAVE, the synthesized failure reasons, and the
  current plan.json; it re-emits this wave and later waves (earlier
  passed waves stay integrated on the issue branch). Increment
  `replan_count`.

## Cleanup is mandatory on EVERY path

Whether the gate PASSes, FAILs, re-plans, or the pipeline blocks/
escalates, the pipeline MUST remove every task worktree it provisioned
for the wave and delete the candidate branch before it returns or loops.
Track the provisioned (worktree path, branch) pairs on the wave row so
none leak. Re-plans use fresh branch/worktree names
(`-w<wave>-r<replan>-<task-id>`) so a leftover never collides.

## Hard cap

- `replan_count` <= 2 per issue. On a third would-be re-plan, STOP:
  return the pipeline result as `status: blocked` with the failure
  reasons and the last gate notes, so the orchestrator surfaces it to
  the maintainer (no unbounded re-planning).

## Hard rules

- Verifiers are READ-ONLY: no edits, no spawns, no PR.
- ASCII only.
- The pipeline is the SOLE writer of the issue branch; verifiers never
  write it.
