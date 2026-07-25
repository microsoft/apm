---
name: shepherd-driver
description: >-
  Use only as the composed drive-to-merge stage of an APM batch
  orchestrator (batch-bug-shepherd, apm-issue-autopilot) that has
  already selected ONE open pull request in microsoft/apm. Do NOT use
  for user-facing requests to triage issues, sweep a queue, or open
  PRs -- the parent orchestrator owns those. Spawn one shepherd-driver
  subagent per PR: it classifies copilot-pull-request-reviewer[bot]
  inline review, runs the apm-review-panel, folds (by default) every
  recommendation inside the PR's stated scope, pushes to the head
  branch or a superseding PR that preserves authorship via commit
  trailers, watches CI to green, and iterates under fixed caps until
  ready-to-merge, advisory-with-deferred, superseded, or blocked. Also
  provides the cross-PR conflict-resolution and mergeability-gate
  phase. This is NOT a standalone entrypoint.
---

# shepherd-driver - per-PR drive-to-merge convergence loop

This SKILL.md is the natural-language module derived from a genesis
design packet; refactors re-run the genesis skill from that packet.

This skill is a COMPOSED BUILDING BLOCK, not a user-facing entrypoint.
It was extracted (genesis R3 EXTRACT) from batch-bug-shepherd when a
second consumer (apm-issue-autopilot) needed the same per-PR
convergence loop. Both orchestrators COMPOSE this skill; neither
re-implements the loop. It in turn COMPOSES the
[apm-review-panel](../apm-review-panel/SKILL.md) skill for the review
pass -- it does NOT re-implement panel review.

## Boundary (what this skill does and does NOT do)

DOES: take ONE PR that already exists and drive it to a terminal
landing-ready state; resolve cross-PR merge conflicts; emit the
PR-facing advisory and supersede comments; return a schema-valid
`completion_return`.

Does NOT: triage issues, decide which issues are worth fixing, open
the first PR for an issue, choose the batch, or maintain the
orchestrator's ground-truth table. Those are the parent
orchestrator's responsibility. If you find yourself triaging or
opening greenfield PRs, you are in the wrong skill.

## How an orchestrator composes this skill

The parent orchestrator, for each PR in its batch, spawns ONE
shepherd-driver subagent with the spawn body in
[assets/shepherd-driver-prompt.md](assets/shepherd-driver-prompt.md).
The orchestrator passes the inputs that prompt declares (PR_NUMBER,
ISSUE_NUMBER, AUTHOR, HEAD_REPO, HEAD_BRANCH, MAINTAINER_CAN_MODIFY,
REPO_ROOT, ORIGIN, optional PANEL_PRIOR). The subagent owns the whole
convergence loop end-to-end and returns a `completion_return`.

After every shepherded PR returns `ready-to-merge`, the orchestrator
runs the conflict-resolution phase: probe mergeability and, on
DIRTY / BEHIND / CONFLICTING, spawn one conflict-resolution subagent
per [assets/conflict-resolution-prompt.md](assets/conflict-resolution-prompt.md).
The step-by-step gate procedure is in
[references/mergeability-gate.md](references/mergeability-gate.md)
(load it when entering that phase).

## Dependency declaration (read before composing)

This skill is a same-repo LOCAL SIBLING. A consuming orchestrator
MUST declare the dependency at its own distribution surface and PROBE
for this skill before spawning. The probe is a tool call, not an
assertion from recall (A9 SUPERVISED EXECUTION; truth #2 CONTEXT
EXPLICIT):

```
test -f ../shepherd-driver/assets/shepherd-driver-prompt.md \
  && test -f ../shepherd-driver/assets/completion-schema.json \
  && test -f ../shepherd-driver/scripts/owner_touch_gate.py \
  && echo "shepherd-driver present" \
  || echo "MISSING shepherd-driver - stop and ask the operator"
```

On a probe MISS the orchestrator stops and asks the operator rather
than re-implementing the loop inline (avoids HAND-ROLLED
HALLUCINATION and PHANTOM DEPENDENCY).

This skill itself COMPOSES [apm-review-panel](../apm-review-panel/SKILL.md).
A consuming orchestrator inherits that transitive dependency; the
spawned shepherd-driver subagent PROBES for it at preflight (all
load-bearing panel assets under
`$REPO_ROOT/.agents/skills/apm-review-panel/`) and returns
`status: blocked` ONLY on a genuine asset MISS, before any checkout
(see the spawn body Step 0.0). Note: a missing `skill` TOOL is NOT a
miss -- in the normal subagent context the panel is executed INLINE
from its on-disk SKILL.md + schemas (Step X.1.1), which is a
first-class path, not a fallback.

## Convergence loop contract (per PR)

The shepherd-driver subagent runs this loop (full detail in the spawn
body):

1. Phase X.0 -- fetch + classify `copilot-pull-request-reviewer[bot]`
   inline review per
   [assets/copilot-classification-prompt.md](assets/copilot-classification-prompt.md).
2. Phase X.1 -- run the `apm-review-panel` against the PR: via the
   `skill` tool if present, otherwise (the normal subagent case)
   execute it INLINE from its on-disk SKILL.md + schemas. Both paths
   produce the same single recommendation comment.
3. Phase X.2 -- merge follow-ups (LEGIT Copilot + panel
   `recommended_followups`) and apply the fold-vs-defer rubric per
   [assets/fold-vs-defer-rubric.md](assets/fold-vs-defer-rubric.md).
4. Phase X.2.5 -- canonical-owner + functional-evidence gate (FAIL
   CLOSED). Run
   [scripts/owner_touch_gate.py](scripts/owner_touch_gate.py) against
   the exact base/head. It parses the single canonical owner table in
   `.apm/instructions/architecture.instructions.md`; no LLM
   self-classification may override a detected touch. Every touched
   owner requires executed exact-head functional test IDs/evidence in
   addition to the boundary lint and any required dual guardrail.
   Schema-validate and semantically verify the version 2 completion
   evidence. Missing evidence stays in the loop or returns `blocked`;
   it is never deferred.
5. Phase X.3 -- edit code; fold every FOLD item. Run the mutation-
   break gate on any new regression-trap test.
6. Phase X.4 -- run the lint contract until silent.
7. Phase X.5 -- push (head branch; fall back to a superseding PR that
   preserves authorship via commit trailers).
8. Phase X.6 -- CI watch + recovery per
   [assets/ci-recovery-checklist.md](assets/ci-recovery-checklist.md).
9. Phase X.7 -- decide terminal vs next iteration.
10. Phase X.8 -- at terminal, capture `head_sha`, `mergeable`,
    `mergeStateStatus`, and CI status for the completion return.

## Caps (hard limits)

- Outer convergence iterations: 4.
- Copilot classification rounds: 2.
- CI recovery iterations: 3.

When a cap is hit with foldable items still open, return
`advisory-with-deferred` with a scope-boundary note per deferred
item. Caps exist to bound non-determinism; they are not targets.

## Terminal returns

The subagent returns exactly one schema-valid `completion_return`
matching [assets/completion-schema.json](assets/completion-schema.json):

- `ready-to-merge` -- clean convergence; CI observed green; lint
  silent; canonical-owner gate passed with schema-valid and
  semantically verified `architecture_evidence` version 2.
- `advisory-with-deferred` -- iteration cap hit with foldable items
  remaining (rare); each deferred item carries a scope-boundary note.
- `superseded` -- push fell back to a superseding PR (records
  `superseded_by`).
- `blocked` -- CI cap hit, panel assets genuinely absent (Step 0.0
  probe miss -- NOT merely the `skill` tool being unavailable), or
  unresolvable scope conflict (records a one-paragraph `blocker`).

The orchestrator schema-validates every return. On malformed, re-spawn
ONCE; on a second malformed return, mark the row blocked and continue.

## Disciplines enforced (inherited by every consumer)

- Fold-by-default: every follow-up inside the PR's stated scope is
  folded into THIS PR; only scope-crossing items are deferred, each
  with a one-line boundary note.
- Mutation-break gate: every regression-trap test added is proven by
  deleting the guard it protects and confirming the test fails
  without it, then restoring the guard.
- Canonical-owner gate: deterministic detection parses the executable
  selectors in the single canonical architecture owner table. Every
  detected owner touch must map to at least one executed functional
  test ID with exact command, passing evidence, and exact head SHA.
  The version 2 completion contract intentionally replaces version 1
  self-classified `decisions[]`; terminal v1 returns are malformed and
  re-spawn once. A new owner, centralization, or split-authority repair
  also requires the full dual guardrail (behavioral regression test,
  static boundary guard, matching `test_architecture_*.py` assertion,
  and mutation-break evidence) plus a clean
  `scripts/lint-architecture-boundaries.sh`. Missing evidence stays in
  the loop or returns `blocked`; it is never deferred.
- Lint contract: the canonical ruff pair
  (`uv run --extra dev ruff check src/ tests/` and
  `ruff format --check src/ tests/`) must be silent before any push.
- CI-observed-green: terminal `ready-to-merge` requires real CI
  evidence, not an assumption.
- One advisory comment per PR per terminal pass, rendered from
  [assets/pr-comment-templates.md](assets/pr-comment-templates.md).
- Authorship preservation: superseding PRs credit the original author
  via commit trailers and link back.

## Consequential writes cross a deterministic tool bridge

Every side effect (push, comment, PR open/close, label, CI read,
mergeability probe) goes through a deterministic CLI (`gh`, `git`,
`uv run ruff`) wrapped in plan + execute + verify (A9 SUPERVISED
EXECUTION). Present-state facts (CI status, mergeable, head sha) are
READ from `gh`/`git` at terminal, never asserted from recall.

## Bundled assets

- [assets/shepherd-driver-prompt.md](assets/shepherd-driver-prompt.md)
  -- the per-PR convergence-loop spawn body. ONE PR per subagent.
- [assets/fold-vs-defer-rubric.md](assets/fold-vs-defer-rubric.md) --
  the fold-by-default decision authority (Phase X.2).
- [assets/copilot-classification-prompt.md](assets/copilot-classification-prompt.md)
  -- LEGIT/NOT-LEGIT classification of Copilot review (Phase X.0).
- [assets/ci-recovery-checklist.md](assets/ci-recovery-checklist.md)
  -- post-push CI watch + recovery loop (Phase X.6; cap 3).
- [assets/conflict-resolution-prompt.md](assets/conflict-resolution-prompt.md)
  -- cross-PR rebase + faithful conflict resolution spawn body.
- [assets/completion-schema.json](assets/completion-schema.json) --
  JSON schema for the `completion` and `conflict-resolution` return
  shapes. Schema-validate every subagent return.
- [scripts/owner_touch_gate.py](scripts/owner_touch_gate.py) --
  non-interactive deterministic owner-touch detection and terminal
  functional-evidence verification. Run `--help` for its CLI.
- [assets/pr-comment-templates.md](assets/pr-comment-templates.md) --
  PR ADVISORY + SUPERSEDE comment shapes (rendered at terminal).
- [references/mergeability-gate.md](references/mergeability-gate.md) --
  load-on-demand step-by-step for the conflict-resolution phase.
