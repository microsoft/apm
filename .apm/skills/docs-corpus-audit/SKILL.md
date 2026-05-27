---
name: docs-corpus-audit
description: >-
  Use this skill to audit the entire microsoft/apm documentation corpus
  against the current implementation, page-by-page, and emit surgical
  fixes for any claim that no longer grounds out true. Activate when
  the maintainer wants a holistic regrounding pass (not a per-PR
  check) -- typically pre-release, post-major-refactor, or when the
  corpus has not been swept in months. Also activate when triggered
  by phrases like "audit the docs", "reground the corpus", "check
  every page against code", or "the docs have drifted -- fix them
  all". The skill is wave-batched, S7-verified, and bounded by
  wave size rather than a flat LLM-call ceiling, so it can scale
  to the full ~112-page corpus. Does NOT replace docs-sync (per-PR
  drift detection) -- they are siblings with different triggers.
  Does NOT auto-merge fixes or push without maintainer review.
---

# docs-corpus-audit -- whole-corpus regrounding pass

The docs corpus drifts silently between releases. `docs-sync` catches
drift introduced by individual PRs at PR-open time. This skill catches
the **accumulated** drift that slips past per-PR review -- stale flag
names, dead nav links from past IA reshuffles, deprecation banners
that outlived their version targets, factual claims whose source-side
truth has moved.

The pattern is **A1 PANEL + WAVE EXECUTION + S7 DETERMINISTIC TOOL
BRIDGE + A9 SUPERVISED EXECUTION**. The corpus is split into disjoint
page scopes; one grounding-verifier subagent owns each scope; agents
extract factual claims, S7-verify against source, and apply surgical
fixes inline. Wave-batched so cost scales with wave size, not flat
call count.

This skill is ADVISORY but ACTIONABLE: agents apply edits inline on
a working branch. The orchestrator is the sole writer to git -- it
stages, commits, and pushes; agents never commit. Maintainer reviews
the resulting PR.

## When to activate

- Pre-release ("we're cutting v0.16 next week -- sweep the docs first").
- Post-major-refactor ("we just reshaped the TOC -- check for dead links").
- Quarterly cadence ("monthly docs drift sweep").
- Maintainer asks for a holistic regrounding pass, OR notices systemic
  drift across multiple pages ("nothing matches the CLI anymore").

## When NOT to activate

- Per-PR docs review -- use `docs-sync` (cheaper, scoped to diff).
- Single-page typo or copy edit -- direct edit is faster.
- Doc-writing for a new feature -- use `docs-impact-architect` + writer.
- Source-code refactors that need doc *advisory* -- use `docs-sync`.

## Architecture invariants

- **Wave-batched, not flat.** Pages are partitioned into 6-8 disjoint
  scopes; each scope is one grounding-verifier subagent. Cost scales
  with wave size, not corpus size. A wave of 6 agents on ~10 pages
  each is the canonical shape.
- **Disjoint page ownership.** Each subagent has EDIT AUTHORITY on
  its scope only. No two agents touch the same file -- guarantees no
  merge conflicts during fan-in.
- **S7 verification is mandatory.** Every factual claim must be
  verified against a deterministic source: `uv run apm <verb> --help`
  for CLI claims, `grep -n src/apm_cli/` for symbols and env vars,
  `python -c "import ..."` for module shape, file existence checks
  for nav links. Never assert from LLM recall.
- **Surgical edits only.** 1-3 line patches per drift, preserving
  voice and structure. Restructuring goes to `would_recommend_adding`
  for orchestrator-side decisioning, never auto-applied.
- **Orchestrator post-pass for cross-corpus patterns.** Some drift
  (e.g. dead nav links from an IA reshuffle) appears across many
  pages and is invisible to a per-scope agent. Orchestrator runs a
  final `grep` sweep for known patterns and patches the residue.
- **Single-writer interlock for git.** Subagents never run `git
  commit`, `git push`, or `gh pr <write>`. Orchestrator commits per
  wave with a structured message and pushes to the working branch.

## Process

```
1. risk-triage          [orchestrator, ~1 LLM call]
   - read .apm/docs-index.yml (NOT the corpus)
   - bucket pages by drift risk: HIGH (CLI ref, schemas, consumer
     flows), MEDIUM (producer, enterprise policy), LOW (concepts,
     contributing, troubleshooting, integrations)
   - decide wave order: HIGH first, MEDIUM next, LOW last
2. wave-planner          [orchestrator, deterministic]
   - partition pages into 6-8 disjoint scopes per wave
   - balance: each agent gets ~9 pages, mixed surface types
3. wave execution        [parallel, one subagent per scope]
   for each agent:
     a. read pages in scope
     b. extract factual claims (CLI flag, env var, file path,
        schema field, behavior assertion, internal nav link)
     c. S7 verify each claim
     d. apply surgical edits inline for DRIFTED claims
     e. return JSON: {pages, claims, grounded, drifted, fixed,
        would_recommend_adding}
4. orchestrator post-pass [orchestrator, deterministic]
   - grep for cross-corpus dead-link patterns
   - patch residue
   - resolve open `would_recommend_adding` items per release-policy
5. commit + push         [orchestrator, single writer]
   - one commit per wave with structured message
   - push to working branch
6. PR + summary comment  [orchestrator]
   - if PR does not exist: open one
   - post wave summary as comment: pages audited, drift caught,
     fixes applied, items deferred
```

## Subagent prompt template

Every grounding-verifier subagent gets this template (orchestrator
substitutes scope + working dir + branch):

```
You are a grounding-verifier subagent in a docs-corpus-audit wave.

**Working directory:** <ABSOLUTE PATH>
**Branch:** <branch-name> (PR #<num> in microsoft/apm, if open)

**Your page scope (EDIT AUTHORITY on these only, ABSOLUTE PATHS):**
- <page 1>
- <page 2>
- ...

**Method per page:**
1. Read the page.
2. Extract every FACTUAL CLAIM: CLI invocation, flag, env var, file
   path, code symbol, config field, exit code, behavior assertion.
3. For each claim, verify against source of truth:
   - CLI claims: `cd <WORKDIR> && uv run apm <verb> --help`
   - File paths / symbols: grep src/apm_cli/
   - Code links (line numbers): cross-check exact line
4. Bucket each claim: GROUNDED | DRIFTED | UNVERIFIABLE.
5. For DRIFTED claims, apply a SURGICAL edit (1-3 line patch,
   preserve voice).
6. NEVER edit files outside your scope. NEVER commit, push, or delete.

**Conventions:**
- ASCII-only rule does NOT apply to docs/src/content/docs/ (Starlight).
- DOES apply to packages/apm-guide/.apm/skills/apm-usage/.
- External-tool commands (gh, codex, claude) = UNVERIFIABLE.

**Return JSON in this shape (no other prose):**
{
  "agent": "<scope-id>",
  "pages": [{"page": "<path>", "claims_checked": N, "grounded": N,
             "drifted": [...], "missing": [...], "edits_applied": N,
             "verdict": "CLEAN|MINOR_DRIFT|MAJOR_DRIFT"}],
  "summary": "...",
  "open_questions": [...]
}
```

## Cost model

| Wave size | Pages | Subagents | LLM calls | Wall time |
|---:|---:|---:|---:|---:|
| Small | ~30 | 4 | ~5 | ~3 min |
| Medium (default) | ~55 | 6 | ~7 | ~5 min |
| Large | ~110 (full corpus) | 12 (two medium waves) | ~14 | ~10 min |

Compare to `docs-sync` (15-call flat ceiling): docs-corpus-audit
scales as O(waves), not O(claims), because the per-agent work fits
in a single context window. The S7 verification cost dominates
wall-time, not LLM cost.

## Cross-corpus post-pass patterns

After waves return, orchestrator runs deterministic greps for known
drift patterns and patches the residue. Maintain this list as you
discover new patterns:

```bash
# IA reshuffle dead links (most common -- updated after each major IA change)
grep -rn "guides/agent-workflows\|introduction/\|guides/install-and-use\|\
guides/pack-distribute\|guides/ci-policy-setup\|guides/compilation\|\
guides/prompts\|guides/dependencies\|guides/drift-detection" \
docs/src/content/docs/

# Stale deprecation version targets (run after every minor release)
grep -rn "removal in v0\.\|will be removed in v0\.\|removed in v0\." \
src/apm_cli/ docs/

# Phantom flag references (manual: cross-check against `apm <verb> --help`
# for any flag mentioned in docs but not in current --help output)
```

## Related primitives

- `docs-sync` -- per-PR drift detection. Sibling skill.
- `docs-impact-classifier` / `docs-impact-localizer` / `docs-impact-architect`
  -- docs-sync's internal classifier-localizer-architect chain.
- `pr-description-skill` -- emits the PR body once fixes are pushed.

## Evals (TBD)

When this skill ships as a maintained primitive, add:
- 2-3 CONTENT EVALS: a corpus snapshot with known seeded drift;
  verify the audit catches it.
- TRIGGER EVALS: 8-10 should-trigger queries ("audit the docs",
  "reground the corpus", "check every page against code", "the docs
  have drifted everywhere") + 8-10 near-miss queries ("review this
  doc PR", "write a guide for X") that should NOT trigger.

## Provenance

This skill was extracted from a real session that audited the
microsoft/apm corpus across 3 waves (PR #1511, 2026-05-27): 112/112
pages audited, 49 surgical fixes, ~25 LLM dispatches, ~30 min wall
time. The session design artifact (genesis hand-off packet) lives at
`references/design-handoff.md` in this skill.
