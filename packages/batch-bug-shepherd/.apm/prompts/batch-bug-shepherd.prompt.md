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
to mergeable PR queue, using the **batch-bug-shepherd** skill as the
working spec. Activate the skill by name -- your harness loads it
from wherever skills live for you (this prompt is harness-agnostic
and makes no assumption about on-disk layout).

Targets for this run: **${input:targets}**

## Procedure

1. ACTIVATE the **batch-bug-shepherd** skill. Treat its contents as
   authoritative for the phase contract (scope -> triage ->
   cross-reference -> shepherd-or-fix -> completion -> final report)
   and the disciplines (verify-before-fix, PR-in-flight detection,
   mutation-break gate, ASCII-only, lint contract, single-writer per
   comment). If the skill is not available in this harness, abort
   with a clear error naming the skill.

2. SCOPE RESOLUTION:
   - If `${input:targets}` is `sweep-all`: run
     `gh issue list --label bug --state open --json
     number,title,labels,body` plus a suspicion-keyword scan on
     untyped open issues.
   - Otherwise: parse the issue numbers from `${input:targets}` and
     fetch each via `gh issue view <n> --json
     number,title,body,labels`.

3. PRINT A BRIEF PLAN to the user BEFORE any fan-out. Include:
   candidate count, wave shape (triage N -> cross-ref -> shepherd k +
   fix m -> completion k+m), the disciplines that will be enforced,
   and where the ground-truth table will live (this session's
   plan.md). If `sweep-all` produced more than 20 candidates, ASK for
   confirmation; otherwise proceed.

   Then RENDER the progress mermaid diagram per the skill's
   `assets/progress-diagram.md` -- every phase styled `pending`,
   with the candidate count `N` substituted into the P0/P1 labels.
   Print the live (empty) ground-truth table directly below it.
   This anchors the operator's view for the rest of the run.

4. INITIALIZE the ground-truth table in plan.md using the
   ground-truth-table asset shipped with the skill. One row per
   candidate. Status `pending-triage`.

5. EXECUTE the skill phases in order. For each phase boundary:
   - reload the ground-truth table from plan.md (B4 PLAN MEMENTO),
   - re-render the progress mermaid with the just-entered phase
     styled `active` and earlier phases `done` / `blocked` /
     `skipped` per the color contract,
   - print the live ground-truth table beneath the diagram,
   - and (for every fan-out wave: Phase 1, 3a, 3b, 4) print the
     dispatch table mapping each subagent_id to its target BEFORE
     spawning.

   These renders are mandatory, not cosmetic -- they are the
   operator's only real-time signal that the saga is alive and
   what it's working on.

6. RENDER the final report from the final-report-template asset
   shipped with the skill at session end. Use clickable GitHub
   issue / PR / author links so the operator can navigate without
   copy-paste. Re-render the progress mermaid one last time with
   every phase `done` (or `blocked` where the human-escalation
   queue is non-empty).

## Delegation

All disciplines (ASCII-only, lint contract, mutation-break gate,
single-writer interlock per PR comment, PR-in-flight cross-reference,
schema-validation of subagent returns) are owned by the
**batch-bug-shepherd** skill. This prompt does NOT re-assert them --
the skill body is the single source of truth. If the skill body
evolves, this prompt inherits the change without edit.
