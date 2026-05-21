---
name: Batch Bug Shepherd
description: Drive a batch of suspected bugs from raw issue list to mergeable PR queue via the batch-bug-shepherd skill
interval: manual
mode: interactive
input:
  - targets: "Either a space-separated issue list (e.g. '#123 #456 #789') OR the literal word 'sweep-all' to expand to every open bug-labeled issue plus untyped issues with bug-suspicion keywords"
---

# Batch Bug Shepherd

Drive a batch of suspected bugs in microsoft/apm from raw issue list
to mergeable PR queue, using the
[batch-bug-shepherd](../skills/batch-bug-shepherd/SKILL.md) skill as
the working spec.

Targets for this run: **${input:targets}**

## Procedure

1. PROBE: confirm `.apm/skills/batch-bug-shepherd/SKILL.md` exists
   in this repository. If missing, abort with a clear error.

2. LOAD the batch-bug-shepherd SKILL.md. Treat it as authoritative
   for the phase contract (scope -> triage -> cross-reference ->
   shepherd-or-fix -> completion -> final report) and the
   disciplines (verify-before-fix, PR-in-flight detection,
   mutation-break gate, ASCII-only, lint contract, single-writer per
   comment).

3. SCOPE RESOLUTION:
   - If `${input:targets}` is `sweep-all`: run
     `gh issue list --label bug --state open --json
     number,title,labels,body` plus a suspicion-keyword scan on
     untyped open issues.
   - Otherwise: parse the issue numbers from `${input:targets}` and
     fetch each via `gh issue view <n> --json
     number,title,body,labels`.

4. PRINT A BRIEF PLAN to the user BEFORE any fan-out. Include:
   candidate count, wave shape (triage N -> cross-ref -> shepherd k +
   fix m -> completion k+m), the disciplines that will be enforced,
   and where the ground-truth table will live (this session's
   plan.md). If `sweep-all` produced more than 20 candidates, ASK for
   confirmation; otherwise proceed.

5. INITIALIZE the ground-truth table in plan.md using
   `.apm/skills/batch-bug-shepherd/assets/ground-truth-table.md` as
   the template. One row per candidate. Status `pending-triage`.

6. EXECUTE the skill phases in order. For each phase boundary,
   reload the ground-truth table before spawning the next wave.

7. RENDER the final report from
   `.apm/skills/batch-bug-shepherd/assets/final-report-template.md`
   at session end.

## Hard rules

- ASCII only in every artifact this prompt produces (plan.md, the
  ground-truth table, PR comments delegated to subagents, the final
  report).
- The orchestrator NEVER posts to a PR directly. Every PR-side write
  is delegated to the responsible subagent (shepherd posts the
  panel comment; completion posts the confirmation comment).
- The lint contract gates every push: completion subagents run
  `uv run --extra dev ruff check src/ tests/ && uv run --extra dev
  ruff format --check src/ tests/` and refuse to push unless both
  are silent.
- The mutation-break gate is non-negotiable: a regression-trap test
  is real only when deleting the production guard makes it FAIL.
- The PR-in-flight cross-reference is non-negotiable: never dispatch
  a fix subagent for an issue that already has an open community PR.
