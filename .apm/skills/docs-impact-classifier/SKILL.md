---
name: docs-impact-classifier
description: >-
  Use this skill to classify the documentation impact of a pull
  request diff, returning one of three verdicts -- no-change,
  in-place edit, or structural change -- with bounded LLM cost.
  Activate as a sibling skill of docs-sync; the orchestrator calls
  this first, before any panel spawn, to keep cost floor at 1 LLM
  call when no docs work is needed. Reads .apm/docs-index.yml as
  the corpus map; never reads the full corpus.
---

# docs-impact-classifier

Single responsibility: given a PR diff and the `.apm/docs-index.yml`
corpus map, emit ONE classification verdict.

This skill is the cost gate for the entire docs-sync system. ~70% of
PRs should exit at verdict `no_change` with zero panel spawn.

## Architecture

This is a 3-layer funnel inside a single skill invocation:

- **L0 deterministic path gate** -- pure file-path matching, no LLM.
- **L1 symbol extraction + corpus grep** -- pure text processing, no LLM.
- **L2 LLM classifier** -- bounded ~8 KB context envelope, 1 call.

The skill returns the verdict from the earliest layer that can decide.

## Step 1: L0 deterministic path gate (no LLM)

Read `.apm/docs-index.yml` to load `no_impact_paths[]` and
`user_surface_paths[]`. Get the changed file list from the PR diff
(`gh pr diff --name-only`).

```
if every changed file matches no_impact_paths AND none match user_surface_paths:
    return {verdict: "no_change", confidence: "high", source: "L0", scope_pages: []}
```

This handles:
- Test-only PRs (`tests/**`)
- CI workflow PRs (`.github/workflows/**`)
- Doc-only PRs (`docs/**`) -- out of scope, docs-sync doesn't review docs PRs
- Primitive-only PRs (`.apm/**`)
- Script and meta PRs

Expected hit rate: ~70% of PRs short-circuit here.

## Step 2: L1 symbol extraction + corpus grep (no LLM)

If L0 did not exit, extract user-observable symbols from the diff:

- **CLI command names** -- grep diff for `^@click.command`, `^@cli.command`, or any `apm <verb>` mention in added/removed lines.
- **Flag names** -- grep diff for `^@click.option`, `--[a-z-]+` patterns.
- **Public API symbols** -- added/removed `def <name>` in `src/apm_cli/__init__.py` or `src/apm_cli/api/**`.
- **Schema keys** -- added/removed keys in `apm.yml`, `apm.lock.yaml`, `apm-policy.yml` parsers.
- **Error strings** -- added/removed string literals in user-facing error paths (look for `_rich_error`, `click.echo`, `raise ... Error(`).

For each extracted symbol, consult `.apm/docs-index.yml#symbol_index`
to find the documented pages. Collect all hits into `candidate_pages[]`.

Also `grep -rn <symbol> docs/src/content/docs/` for symbols NOT in
the index (catches drift between index and corpus).

## Step 3: L2 LLM verdict (1 call, bounded context)

If L1 found zero candidate pages AND zero schema/CLI/flag changes:
return `{verdict: "no_change", confidence: "medium", source: "L1", scope_pages: []}`.

Otherwise, invoke the doc-analyser persona with EXACTLY this context
envelope (must fit in ~8 KB tokens):

- PR title + body (first 500 chars)
- Diff stats (`gh pr diff --stat` output)
- `.apm/docs-index.yml` (the whole file; it's ~8 KB seeded, may grow)
- L1 candidate pages with +/-5 lines of context per hit
- Path-classification summary from L0
- **`pr_doc_diff_paths[]`**: the list of paths under `docs/src/content/docs/**`
  that the PR itself already modifies (drives the `in_place_resolved`
  downgrade rule in "In-place-resolved detection" below).

Ask doc-analyser to return JSON matching this schema:

```json
{
  "verdict": "no_change" | "in_place_resolved" | "in_place" | "structural",
  "confidence": "low" | "medium" | "high",
  "scope_pages": ["docs/src/content/docs/..."],
  "structural_proposal": {
    "new_pages": [{"slug": "...", "rationale": "..."}],
    "moved_pages": [{"from": "...", "to": "..."}],
    "toc_changes": "<one-paragraph>"
  },
  "reasoning": "<one-paragraph: what surface changed, what docs are affected, why this verdict>"
}
```

`structural_proposal` is populated only when verdict is `structural`.
`scope_pages` is populated for `in_place` and `structural` verdicts.

## Verdict semantics

| Verdict | Meaning | Panel size | Cost |
|---|---|---|---|
| `no_change` | No user-observable surface changed | 0 panel spawns | ~0-1 LLM call |
| `in_place_resolved` | Doc impact existed, but the PR's OWN diff already patches every page in `scope_pages` -- author already did the work | 0 panel spawns; skill emits NO advisory | ~1 LLM call |
| `in_place` | One to a few pages need a paragraph or section update; no new pages, no TOC change | N candidate pages x (doc-writer + python-architect) + editorial-owner + growth-hacker + CDO | ~6-12 LLM calls |
| `structural` | A new page is needed, OR an existing page should be split/merged, OR the TOC needs to change to fit a new concept | architect first (TOC delta), then in-place panel for affected pages | ~10-15 LLM calls |

## In-place-resolved detection (false-alarm killer)

BEFORE returning `in_place`, intersect your `scope_pages[]` with the
list of files the PR itself touches under `docs/**` (provided to you
by the orchestrator under `pr_doc_diff_paths[]`). If EVERY scope page
already appears in `pr_doc_diff_paths`, downgrade to `in_place_resolved`
and emit `reasoning` of the form "Author already patched <page list>".
This is the well-behaved-author path; the skill stays silent.

If only SOME scope pages are pre-patched, keep `in_place` and list the
REMAINING (unpatched) pages in `scope_pages[]`. Note the pre-patched
ones in `reasoning` for transparency.

## Rename / breaking-change heuristic (PR 1244 class)

When the L1 layer reports an ADDED public symbol that matches an
EXISTING public symbol's name in the corpus (e.g. PR adds `apm update`
but `apm update` already appears in 9 docs pages with different
semantics), this is a RENAME or BREAKING SEMANTIC CHANGE. Bias toward
`structural` (not `in_place`):
- the existing page describing the OLD semantics may need to SPLIT
  into two pages (old verb under new name + new verb keeping old name)
- the TOC may need a NEW reference page for the renamed verb
- every passing mention in the corpus needs verification

Do NOT collapse a rename into `in_place` just because the affected
pages already exist. The shape of the work is structural even when no
new page is strictly required.

## Anti-patterns (verdict shape errors)

- Returning `in_place` with empty `scope_pages` -- invalid; orchestrator will reject.
- Returning `structural` without `structural_proposal` -- invalid.
- Returning `in_place` when EVERY scope page is in `pr_doc_diff_paths` -- should be `in_place_resolved`.
- Inflating `structural` to seem thorough -- the CDO will catch this. Return the minimal true verdict.
- Missing the rename heuristic above and emitting `in_place` for a verb-swap PR.
- Reading the corpus (the .md files themselves) at L2 -- context budget breach. You read the index, not the corpus.

## Output contract

Return a SINGLE JSON document matching the schema in Step 3 as the
final message of your task. No prose around the JSON. The
orchestrator parses your last message.
