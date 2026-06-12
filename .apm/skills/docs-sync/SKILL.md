---
name: docs-sync
description: >-
  Use this skill whenever a pull request is opened, reopened, or
  synchronized in microsoft/apm to assess whether and how the
  documentation corpus must change to stay truthful with the
  proposed code change. Activate even when the PR title or body
  says nothing about docs -- the skill must run on every PR to
  detect silent drift between code and docs. Classifies impact
  as no-change, in-place edit (one to a few paragraphs), or
  structural change (new page or TOC reshape), then orchestrates
  a CDO + doc-writer + python-architect + editorial-owner +
  growth-hacker loop to produce a patch-ready advisory. Does NOT
  review code quality, security, or test coverage. Does NOT
  auto-merge or auto-push doc edits.
---

# docs-sync -- per-PR documentation impact panel

The docs corpus drifts silently and constantly. This skill catches
drift at PR-open time, classifies its impact, and orchestrates a
persona panel to produce a patch-ready advisory comment.

The pattern is **A1 PANEL + B1 FAN-OUT/SYNTHESIZER + A8 ALIGNMENT
LOOP**. The classifier is the cost gate (~70% of PRs short-circuit
to no-change with ~1 LLM call). When the panel does fan out, every
agent reads a bounded context (~10 KB) -- never the full corpus.

This skill is ADVISORY. It does not gate merge, apply verdict
labels, or push to the contributor's fork. The orchestrator is the
sole writer to the PR: exactly one comment per run (idempotent
edit-in-place), plus optional label sweeps.

## Architecture invariants

- **Cost ceiling: 15 LLM calls per run.** Hard-wired. The orchestrator refuses to spawn beyond. Header prints `N/15` for observability.
- **Single-writer interlock.** Only the orchestrator writes. Panelist subagents return JSON; they MUST NOT call any `gh` write command, post comments, or touch PR state.
- **Idempotent comment.** Exactly one comment per run, with a stable header `## Docs sync advisory`. Re-runs edit-in-place using `gh pr comment --edit-last`.
- **No fork-write.** Companion docs PRs (only on structural verdict with `docs-sync-confirm` label) open from a bot branch in the BASE repo; never pushed to the contributor's fork.
- **Index-not-corpus reads.** Every classifier and architect agent reads `.apm/docs-index.yml`, NOT the corpus itself. The corpus is sampled only by the localizer (which reads the specific candidate pages) and by per-page panelists (which read one page each).
- **S7 deterministic tool bridge.** The python-architect panelist MUST run real `apm --help`, `grep`, and `python -c` commands to verify doc claims, never assert from prose.

## Roster

| Role | Agent | Always active? |
|---|---|---|
| Classifier | [doc-analyser](../../agents/doc-analyser.agent.md) inside [docs-impact-classifier](../docs-impact-classifier/SKILL.md) | Yes (every run) |
| Localizer | [docs-impact-localizer](../docs-impact-localizer/SKILL.md) | Only on `in_place` verdict |
| Architect | [docs-impact-architect](../docs-impact-architect/SKILL.md) | Only on `structural` verdict |
| Writer | [doc-writer](../../agents/doc-writer.agent.md) | Per candidate page (fan-out) |
| Verifier | [python-architect](../../agents/python-architect.agent.md) | Per candidate page (fan-out, S7) |
| Editorial | [editorial-owner](../../agents/editorial-owner.agent.md) | Once across all redrafts |
| Growth | [oss-growth-hacker](../../agents/oss-growth-hacker.agent.md) | Once across all redrafts |
| Synthesizer | [cdo](../../agents/cdo.agent.md) | Once, with ALIGNMENT LOOP up to 3 redrafts |

## Topology

```
   docs-sync SKILL (orchestrator thread)
                 |
   Step 1: classify (1 LLM call, may exit here)
                 |
                 v
            verdict?
            /    |    \
   no-change  in-place  structural
       |        |          |
     EXIT       |       architect (TOC delta)
                |          |
                +----<-----+
                |
   Step 2: localize (1 LLM call) -- per-page task brief
                |
   Step 3: FAN-OUT panel via task tool
                |
       +----+----+----+----+
       v    v    v    v    v
     writer  verify edit growth
     x N    x N   once  once
       (parallel; each <=10 KB context)
                |
   Step 4: schema-validate returns
                |
   Step 5: CDO synthesize (1 LLM call)
                |
            agree?
            / | \
        revise (N<=3 redrafts) | agree
                                  |
   Step 6: emit ONE comment via safe-outputs.add-comment
   Step 7: OPTIONAL companion docs PR (only if structural AND
           `docs-sync-confirm` label present)
```

## Execution checklist

### Step 1 -- Classify

Spawn ONE task: load the `docs-impact-classifier` skill, pass it the
PR number. It returns the classifier JSON.

Validate the JSON against `assets/classifier-return-schema.json`.
On schema failure, abort the run with a comment explaining the
internal error.

If verdict is `no_change`: skip to Step 6 with a brief advisory
("No docs impact detected. Reason: <one-line>. LLM calls: 1/15.")

### Step 2 -- Localize (in_place) or Architect (structural)

For `in_place`: spawn ONE task that loads the
`docs-impact-localizer` skill with the classifier output. Returns
per-page task briefs.

For `structural`: spawn ONE task that loads the
`docs-impact-architect` skill with the classifier output. Returns
TOC delta + new-page outlines + downstream in-place pages. THEN
spawn the localizer for those downstream pages.

### Step 3 -- Fan-out panel

**Cascade-size mitigation (PR 1244 class).** If `scope_pages[]` has
>8 entries, the per-page fan-out at one writer call per page would
approach the 15-call ceiling with no headroom for verifier redrafts.
BEFORE spawning, group `scope_pages[]` into SECTIONS:

- Pages under the same TOC section (e.g. all `consumer/**`) with the
  SAME conceptual fix (e.g. "rename apm update -> apm self-update in
  every mention") become ONE writer task with a `pages_in_section[]`
  array in its brief.
- A 9-page rename cascade collapses to 2-3 section writer tasks.

The python-architect verifier still runs per `verify_claims[]` (not
per page), because S7 evidence is keyed on claims, not pages.

For each page-or-section in the per-page task brief, spawn TWO parallel tasks:

1. **doc-writer** task -- drafts the patch for that page's (or section's) specific edits. Output: JSON with `before:`, `after:` for each location.
2. **python-architect** task -- for each `verify_claims[]` in the page brief, run the actual command (S7 tool bridge: `apm <verb> --help`, `grep -n <symbol> src/`). Output: JSON with `claim: verified | refuted | inconclusive` per claim.

In parallel with the per-page fan-out, spawn ONCE each:

3. **editorial-owner** task -- receives ALL writer drafts, returns tone fixes.
4. **oss-growth-hacker** task -- receives ALL writer drafts, returns ramp-clarity notes (does this read well to a cold OSS visitor).

All panelist tasks return JSON matching `assets/panelist-return-schema.json`.
Schema-validate every return; on failure, abort.

### Step 4 -- Validate

Cross-check:

- Every `verify_claims` from a python-architect comes back `verified` or `inconclusive` (never `refuted`). If any are `refuted`, the doc-writer's draft is wrong; re-run the writer for that page with the refutation as context.
- Cross-page constraints from the localizer are honored across all writer drafts.
- All drafts are ASCII-only (per repo encoding rule).

### Step 5 -- CDO synthesize

Spawn ONE task: load the `cdo` persona with the full panel return
(writer drafts + verifier reports + editorial notes + growth notes
+ classifier verdict + (architect output if structural)) and
`.apm/docs-index.yml`.

The CDO returns one of three verdicts:

- `agree`: ship. Proceed to Step 6.
- `revise`: re-spawn the writer panelists with the CDO's specific
  concerns as additional context. Re-run the editorial and growth
  passes if needed. Bounded N <= 3 redrafts. Increment a redraft
  counter; if it hits 3 and CDO still disagrees, ship with
  `cdo_disagreement_noted: true`.
- `ship_with_disagreement`: ship as-is with the disagreement
  surfaced in the comment for the maintainer to weigh.

### Step 6 -- Emit ONE comment

Render `assets/advisory-comment-template.md` with the final results.
Write it via `safe-outputs.add-comment`. Header is exactly
`## Docs sync advisory` (stable for idempotent edit-in-place).

The comment MUST include the cost header:

```
Verdict: <verdict>  *  Pages affected: N  *  LLM calls: M/15  *  Took: Xs
```

### Step 7 -- Optional companion PR

Only on `structural` verdict AND `docs-sync-confirm` label present
on the PR (the A9 SUPERVISED EXECUTION boundary; the maintainer
ratifies the structural proposal before any PR is opened).

If both conditions hold:

1. Branch name: `docs-sync/companion-<PR_NUMBER>` in the BASE repo.
2. Apply the doc-writer drafts as a commit on that branch.
3. Apply the architect's TOC delta (`.apm/docs-index.yml` entries +
   new page files + redirects on retired pages).
4. Open a draft PR linked to the original PR, with the advisory
   comment text as the PR body.
5. Reference the companion PR in the advisory comment.

This step is intentionally GATED. The default behaviour (no
`docs-sync-confirm` label) is to recommend the patches in the
comment without opening a PR.

## Cost accounting

The orchestrator maintains a running LLM-call counter:

| Step | Min calls | Max calls |
|---|---|---|
| Step 1 classify | 1 | 1 |
| Step 2 localize/architect | 0 | 2 |
| Step 3 fan-out (N pages) | 0 | 2N + 2 |
| Step 5 CDO | 0 | 1 + 3 redrafts |
| Total | 1 | 15 |

If the counter would exceed 15, the orchestrator stops spawning,
ships the partial result with `cost_ceiling_hit: true`, and the
comment surfaces the truncation.

## Anti-patterns

- Reading the corpus instead of the index. Context budget breach.
- Letting panelists post comments. Single-writer interlock violation.
- Ignoring `refuted` verify_claims. That's silent drift you're shipping.
- Skipping the CDO synthesis on "obvious" in-place patches. The bridges still matter.
- Auto-opening companion PRs without the confirm label. Removes the human ratification.
- Re-running on every push (synchronize). Wasteful. Re-apply the trigger label for re-run.

## Operating modes

- **Rung 1 (label-gated, default)**: triggered by `docs-sync` label on PR. Maintainer opts in.
- **Rung 2 (default-on)**: triggered on every `pull_request_target` event. Enabled only after shadow validation.

The workflow file controls which rung is active. The skill body is
identical for both.
