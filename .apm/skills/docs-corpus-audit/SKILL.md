---
name: docs-corpus-audit
description: >-
  Use this skill to run a holistic regrounding pass on the entire
  microsoft/apm documentation corpus against current source code,
  page-by-page, and emit surgical fixes for stale claims. Activate
  when the maintainer wants a WHOLE-CORPUS audit (not per-PR review)
  -- typical triggers include "audit the docs", "reground the
  corpus", "check every page against code", "pre-release docs
  sweep", "the docs have drifted everywhere", or "we just reshaped
  the TOC, find dead links". Wave-batched and S7-verified; scales
  to the full ~112-page corpus in ~10 minutes wall-time. This is a
  SIBLING to docs-sync, not a replacement: docs-sync is per-PR
  (triggered by a diff); this skill is per-corpus (triggered by a
  maintainer ask). They share agent personas, schemas, and the
  docs index, but their triggers MUST NOT collide. Does NOT
  auto-merge, does NOT push without maintainer review, and does
  NOT replace per-PR drift detection.
---

# docs-corpus-audit -- whole-corpus regrounding pass

The docs corpus drifts silently between releases. `docs-sync` catches
drift introduced by individual PRs at PR-open time. This skill catches
the **accumulated** drift that slips past per-PR review -- stale flag
names, dead nav links from past IA reshuffles, deprecation banners
that outlived their version targets, factual claims whose source-side
truth has moved.

The pattern is **A1 PANEL + WAVE EXECUTION + S7 DETERMINISTIC TOOL
BRIDGE + A8 ALIGNMENT LOOP + A9 SUPERVISED EXECUTION**. The corpus is
split into disjoint page scopes; one verifier subagent owns each
scope; agents extract factual claims, S7-verify against source, apply
surgical fixes inline. The orchestrator then runs an alignment-loop
pass to re-verify that applied edits actually ground out true.

This skill is ADVISORY but ACTIONABLE: agents apply edits inline on a
working branch. The orchestrator is the sole writer to git -- stages,
commits, pushes. Maintainer reviews the resulting PR.

## Sibling contract with docs-sync

These two skills share substrate. Be explicit:

| Shared resource | Owner | Both use |
|---|---|---|
| `.apm/docs-index.yml` (corpus map) | docs-sync | yes |
| [doc-writer](../../agents/doc-writer.agent.md) persona | shared | yes (per-page edits) |
| [python-architect](../../agents/python-architect.agent.md) persona | shared | yes (S7 verification) |
| [editorial-owner](../../agents/editorial-owner.agent.md) persona | shared | optional (voice pass at scale) |
| [cdo](../../agents/cdo.agent.md) persona | shared | yes (final synthesis) |
| `assets/panelist-return-schema.json` | docs-sync (mirrored) | yes |

**Trigger boundary (avoid DISPATCH COLLISION):**

- `docs-sync` triggers on a PR event ("PR opened/synchronized",
  source-diff-driven).
- `docs-corpus-audit` triggers on a maintainer ask for a
  WHOLE-CORPUS pass ("audit the corpus", "reground", "pre-release
  sweep") -- no PR required, no diff required, the whole corpus
  is the input.

If a maintainer asks "review this PR's doc impact", route to
`docs-sync`. If they ask "audit all our docs" or "the docs feel
stale everywhere", route here.

## Architecture invariants

- **Wave-batched, not flat.** Pages are partitioned into 6-8 disjoint
  scopes; each scope is one verifier subagent. Cost scales with
  wave size, not corpus size. A wave of 6 agents on ~10 pages each
  is the canonical shape.
- **Disjoint page ownership.** Each subagent has EDIT AUTHORITY on
  its scope only. No two agents touch the same file -- guarantees
  no merge conflicts during fan-in.
- **S7 verification is mandatory.** Every factual claim is verified
  against deterministic source: `uv run apm <verb> --help` for CLI,
  `grep -n src/apm_cli/` for symbols, `python -c "import ..."` for
  module shape, file-existence checks for nav links. Never assert
  from LLM recall.
- **Surgical edits only.** 1-3 line patches per drift, preserving
  voice. Restructuring is deferred to the orchestrator post-pass,
  never auto-applied by per-scope agents.
- **Single-writer interlock for git.** Subagents NEVER run
  `git commit`, `git push`, or `gh pr <write>`. Orchestrator
  commits per wave; pushes once per session.
- **Alignment loop (A8).** After waves return, orchestrator
  re-greps the corpus for the patterns the agents claimed to fix.
  Any residue triggers a targeted re-dispatch (max 2 redrafts) or
  is escalated to maintainer.

## Roster (composition, not invention)

Reuse docs-sync's personas. Do NOT invent a one-off "grounding-
verifier" role; that's R3 EXTRACT in reverse.

| Role | Persona | Always active? |
|---|---|---|
| Per-scope verifier+editor | [python-architect](../../agents/python-architect.agent.md) (S7) and [doc-writer](../../agents/doc-writer.agent.md) (edits), bundled into one subagent prompt per scope | Yes -- one per page scope, parallel fan-out |
| Cross-corpus post-pass | orchestrator (deterministic greps via `scripts/scan-cross-corpus-drift.sh`) | Yes -- once after waves return |
| Alignment-loop checker | orchestrator (deterministic re-grep + targeted re-dispatch) | Yes -- once after post-pass |
| Voice pass (optional) | [editorial-owner](../../agents/editorial-owner.agent.md) | Only when >20 edits to keep tone coherent |
| Final synthesis | [cdo](../../agents/cdo.agent.md) | Once, for the PR summary comment |

The per-scope subagent prompt that composes `python-architect` +
`doc-writer` is in `assets/subagent-prompt-template.md` -- the
orchestrator substitutes scope + working dir + branch and dispatches
via the task tool.

## Process

```
1. PROBE (A9 SUPERVISED EXECUTION)
   - Check working tree: docs/src/content/docs/ exists?
   - Check working tree: packages/apm-guide/.apm/skills/apm-usage/
     exists? (Rule-4 backfill target. If missing, the audit cannot
     close Rule 4; ask maintainer before continuing.)
   - Check `.apm/docs-index.yml` reachable.
   - Verify on a working branch (not main).

2. RISK-TRIAGE (orchestrator, ~1 LLM call)
   - Read .apm/docs-index.yml only (NOT the corpus body).
   - Bucket pages by drift risk: HIGH (CLI ref, schemas, consumer
     flows), MEDIUM (producer, enterprise policy), LOW (concepts,
     contributing, troubleshooting, integrations).
   - Decide wave order: HIGH first, MEDIUM next, LOW last.

3. WAVE-PLANNER (orchestrator, deterministic)
   - Partition pages into 6-8 disjoint scopes per wave.
   - Each agent gets ~9 pages, mixed surface types.

4. WAVE EXECUTION (parallel, one subagent per scope)
   - Orchestrator dispatches one task per scope using the prompt
     template in assets/subagent-prompt-template.md.
   - Subagents read pages, extract claims, S7-verify, apply
     surgical edits, return JSON per the docs-sync panelist
     schema (mirrored at assets/panelist-return-schema.json).
   - Validate every return against the schema; reject malformed
     JSON.

5. CROSS-CORPUS POST-PASS (orchestrator, deterministic)
   - Run scripts/scan-cross-corpus-drift.sh to grep for patterns
     a per-scope agent cannot see (IA-reshuffle dead links, stale
     deprecation version targets, phantom flag references).
   - Patch residue inline.

6. ALIGNMENT LOOP (orchestrator, deterministic)
   - Re-run scripts/scan-cross-corpus-drift.sh.
   - Re-grep for claims the agents marked DRIFTED-FIXED.
   - If residue: targeted re-dispatch to the owning agent
     (bounded: max 2 redrafts per wave).

7. COMMIT + PUSH (orchestrator, single writer)
   - One commit per wave; structured message naming closed items.
   - Push to working branch.

8. PR + SUMMARY COMMENT (orchestrator)
   - If no PR exists: open one with the [pr-description-skill]
     (../pr-description-skill/SKILL.md).
   - Post per-wave summary comment: pages audited, drift caught,
     fixes applied, items deferred, alignment-loop residue.
```

## Bundled assets

- `assets/subagent-prompt-template.md` -- the per-scope prompt the
  orchestrator substitutes and dispatches. Composes python-architect
  (S7) + doc-writer (surgical edit). Loaded once per scope.
- `assets/panelist-return-schema.json` -- subagent return schema,
  mirrored from docs-sync. Loaded once at wave start; validated
  against every return.
- `scripts/scan-cross-corpus-drift.sh` -- deterministic grep sweep
  for cross-corpus patterns (IA dead links, stale deprecation
  targets, phantom flags). Non-interactive; emits structured
  matches on stdout, diagnostics on stderr. Run `--help` for
  pattern list. Update this script after each major IA reshuffle.

## Cost model

| Wave size | Pages | Subagents | LLM dispatches | Wall time |
|---:|---:|---:|---:|---:|
| Small | ~30 | 4 | ~5 | ~3 min |
| Medium (default) | ~55 | 6 | ~7 | ~5 min |
| Large | ~110 (full corpus) | 12 (two medium waves) | ~14 | ~10 min |

Compared to docs-sync (15-call flat ceiling), this skill scales as
O(waves), not O(claims), because per-agent work fits in one context
window. S7 verification dominates wall-time, not LLM cost.

## Boundary (what this skill does NOT do)

- Per-PR doc-impact review -- use `docs-sync`.
- Single-page typo or copy edit -- direct edit is faster.
- Writing docs for a brand-new feature -- use `docs-impact-architect`
  and `doc-writer` directly.
- Auto-merging or pushing without maintainer review.
- Reviewing code quality, security, or test coverage (out of scope).

## Evals

See `evals/`:
- `evals/content-evals.json` -- 3 corpus snapshots with seeded drift
  (stale CLI flag, dead nav link, expired deprecation target);
  expected behavior is that the skill catches all three and applies
  surgical fixes that ground out true on re-verification.
- `evals/trigger-evals.json` -- 10 should-trigger + 10 should-NOT-
  trigger queries, 60/40 train/val. The val split is the ship gate
  (>=0.5 should-trigger AND <0.5 should-not-trigger).
- `evals/README.md` -- how to run.

## Provenance

This skill was extracted from a real session that audited the
microsoft/apm corpus across 3 waves (PR #1511, 2026-05-27):
112/112 pages audited, 49 surgical fixes, ~25 LLM dispatches,
~30 min wall-time. The session design artifact (genesis hand-off
packet) lives in session state, not in this bundle (maintainer-
scope, not runtime-loaded).
