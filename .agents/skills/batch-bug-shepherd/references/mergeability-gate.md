# Mergeability gate (Phase 5) - orchestrator-side reference

Load this file when entering Phase 5 of the batch-bug-shepherd
session. It is the load-on-demand expansion of the Phase 5 contract
in SKILL.md; the body keeps the contract terse, this file holds the
step-by-step.

The Phase 5 gate exists because a PR marked READY-TO-MERGE at end
of Phase 4 can stop being mergeable five minutes later when the
maintainer merges something else into main. Mergeability is
POST-WAVE truth, not pre-wave assumption. The skill must not assert
"ready" without re-probing.

## Architecture invariant (inherited from SKILL.md)

Mergeability is the last sentence the orchestrator says, not the
first. Phase 4 produces a ready-to-merge CANDIDATE list; Phase 5
produces the ready-to-merge VERIFIED list.

Sub-phase topology:

- 5a (read-only, single thread): probe every Phase-4 ready PR via
  `gh pr view --json mergeStateStatus,mergeable,maintainerCanModify`.
  Partition into MERGEABLE vs CONFLICTING.
- 5b (fan-out, one subagent per CONFLICTING PR): dispatch the
  `conflict-resolution-prompt.md` spawn body. WAIT for all returns.
- 5c (read-only, single thread): re-probe every PR that was in 5b,
  synthesize the four-way partition (resolved /
  requires-author-action / requires-human-judgment /
  resolution-failed), update the ground-truth table.

If the Phase-4 ready list is empty, skip Phase 5 entirely and
render P5a/P5b/P5c as `skipped` in the progress diagram.

## Sub-phase 5a - probe loop

For each PR in the Phase-4 `ready_to_merge` set:

```
gh pr view <PR_NUMBER> --json \
    number,mergeStateStatus,mergeable,maintainerCanModify,\
    headRepository,headRepositoryOwner,headRefName \
  --jq '{n:.number, ms:.mergeStateStatus, mg:.mergeable,\
         mcm:.maintainerCanModify, ho:.headRepositoryOwner.login,\
         hn:.headRepository.name, hb:.headRefName}'
```

Partition by `mergeStateStatus`:

- CLEAN / UNSTABLE / HAS_HOOKS -> goes to the verified-ready
  bucket (drop straight into Phase 6's `ready_to_merge` list).
- BEHIND -> goes to 5b (rebase will make it CLEAN; conflicts may
  or may not surface during the rebase itself).
- DIRTY / CONFLICTING -> goes to 5b (conflicts certain).
- UNKNOWN / null -> retry up to 3 times with 10s backoff (GitHub
  is still computing). If still UNKNOWN after retries, treat as
  CONFLICTING and route to 5b.
- BLOCKED -> NOT a conflict; this is a CI / required-review gate.
  Leave in `ready_to_merge` with a `gate_note` field. Phase 5 does
  not chase Approve-and-run or required-review state.

Print the dispatch table (per `progress-diagram.md` "Dispatch-time
table requirement") BEFORE spawning 5b, even if `C = 0` (in which
case print "Phase 5b skipped: 0 CONFLICTING PRs").

## Sub-phase 5b - fan-out conflict resolution

For each CONFLICTING PR, spawn ONE subagent using the
`conflict-resolution-prompt.md` spawn body. Subagents run in
parallel. Each subagent owns one PR end-to-end (rebase, lint,
mutation-break re-check, push, re-probe, comment).

Pass these inputs from the 5a probe into the spawn body:

| spawn input              | source                                          |
|--------------------------|-------------------------------------------------|
| PR_NUMBER                | 5a probe `.number`                              |
| ISSUE_NUMBER             | ground-truth table row                          |
| AUTHOR                   | ground-truth table row                          |
| HEAD_REPO                | `<ho>/<hn>` from 5a probe                       |
| HEAD_BRANCH              | `<hb>` from 5a probe                            |
| MAINTAINER_CAN_MODIFY    | `<mcm>` from 5a probe                           |
| REPO_ROOT                | orchestrator session cwd                        |
| ORIGINAL_MERGE_STATE_... | `<ms>` from 5a probe                            |
| CONFLICTING_PATHS_HINT   | optional; from a `git fetch` + `git merge-tree` |

WAIT for ALL 5b subagents to return before entering 5c. Do not
start the synthesis early -- the four-way partition needs every
return.

## Sub-phase 5c - re-probe synthesis

For each PR that returned from 5b, re-probe via the same
`gh pr view --json mergeStateStatus,mergeable` command. The
subagent already re-probed at the end of step 8 of its spawn body;
this is a TRUST-BUT-VERIFY re-probe so the orchestrator's view of
truth is independent of the subagent's claim.

Partition the 5b returns into four buckets matching the schema's
four return statuses:

| status                    | route to final-report section            | additional action |
|---------------------------|------------------------------------------|--------------------|
| resolved                  | `ready_to_merge` (joined with 5a CLEAN)  | none; subagent already posted resolution-confirmation comment |
| requires-author-action    | `requires_author_action` section         | none; the rebase was never attempted (push-permission gate) |
| requires-human-judgment   | `requires_human_judgment` section        | none; subagent already recorded the conflicting paths |
| resolution-failed         | `resolution_failed` section              | none; subagent's blocker explanation lands in the report |

Update the ground-truth table: change `status` on each row from
`ready-to-merge` to one of the four post-gate values. Re-render the
table + progress diagram (P5c done, P6 active).

## Trust-but-verify discipline

The orchestrator's 5c re-probe is mandatory even when the subagent
returned `status: resolved` with `mergeStateStatus_post: CLEAN`.
Two reasons:

1. Mergeability is a fact-that-must-be-true (truth #2 CONTEXT
   EXPLICIT). LLM-asserted CLEAN is hallucination-shaped. The only
   ground truth is the live API.
2. The subagent re-probe at its step 8 happened ~30s after the
   push. Another merge to main could have landed in that window.

If the orchestrator re-probe disagrees with the subagent claim,
the orchestrator's value wins. The subagent's
`mergeStateStatus_post` is its self-report; the orchestrator's
re-probe is the gate.

## What this phase does NOT do

- Does NOT chase BLOCKED (required-review, Approve-and-run). Those
  belong to the maintainer's policy workflow, not to the bbs.
- Does NOT re-run the apm-review-panel. Phase 3 owns shepherding;
  Phase 4 owns follow-up implementation; Phase 5 owns ONLY rebase
  and re-probe. Conflict resolution is a mechanical merge, not a
  fresh judgment pass.
- Does NOT open new PRs. Supersession is a Phase 4 affordance; if
  a fork-with-flag-false PR shows up here, it routes to
  `requires-author-action`, period. The maintainer decides whether
  to supersede.
- Does NOT post more than one comment per PR. The Phase 4
  completion-confirmation comment plus the Phase 5b
  resolution-confirmation comment is the cap. No third comment
  from any phase under any circumstance.
