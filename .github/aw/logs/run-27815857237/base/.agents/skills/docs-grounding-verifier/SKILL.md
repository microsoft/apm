---
name: docs-grounding-verifier
description: Use this skill to verify CLAIM-LEVEL grounding of a documentation page (or set of pages) against the source code. Activate when you have specific pages to check for factual accuracy -- not when sweeping a whole corpus (use docs-corpus-audit for that) and not when triaging a PR diff (use docs-sync for that). Trigger nouns: "is this doc accurate", "verify the page against the code", "fact-check this section", "any claims that drifted from source", "fact-checking", "grounding audit", "drift hunt", "claim verification". Returns per-claim verdicts (GROUNDED | PARTIAL | CONTRADICTED | UNSUPPORTED) with file:line evidence citations. Catches paragraph-level inaccuracies that page-level audit averages over -- e.g. a paragraph with 5 claims where 4 are grounded and 1 is fabricated. Does NOT modify files (returns advisory only); does NOT re-architect the docs; does NOT triage PRs.
---

# docs-grounding-verifier

CLAIM-LEVEL grounding verification. Adapts the RAGAS faithfulness-eval
pattern (proven in RAG literature) to docs/code instead of generated-
answers/retrieved-context. Source code is the ground truth; docs
paragraphs are the candidate text under audit.

[python-architect persona](../../personas/python-architect.persona.md)
[doc-writer persona](../../personas/doc-writer.persona.md)

## Sibling contract

This skill is a SIBLING of `docs-corpus-audit` and `docs-sync`. The
boundary is load-bearing:

| Skill                  | Trigger                                | Scope            | Granularity     |
| ---------------------- | -------------------------------------- | ---------------- | --------------- |
| docs-sync              | PR opened/synchronized                 | PR diff only     | Page-level      |
| docs-corpus-audit      | Maintainer asks for whole-corpus pass  | Entire corpus    | Page-level      |
| **docs-grounding-verifier** | **Verify specific pages factually**    | **1..N pages**    | **CLAIM-level** |

`docs-corpus-audit` invokes this skill in its VERIFY phase on the
highest-risk pages of each wave. `docs-sync` can invoke it on the
specific pages in a PR diff. The skill is also runnable standalone.

## When to activate

- Maintainer says "verify <page> against the code".
- An audit wave wants per-claim grounding scores for its highest-risk pages.
- A PR review wants to confirm that prose changes are not just plausible
  but actually consistent with the implementation.
- A "fact-check" or "grounding" or "drift hunt" request.

## When NOT to activate

- Whole-corpus sweep with no specific page list -> use `docs-corpus-audit`.
- PR review with mixed code+docs diff -> use `docs-sync`.
- Editorial / tone review -> use `editorial-owner` persona directly.

## Architecture (PIPELINE-of-PANELS)

```
PARENT
  -> [Stage 1: EXTRACT claims, fan-out PANEL]
       per page -> LLM extracts atomic factual claims as JSON
       script: scripts/extract-claims.py
  -> [Stage 2: RETRIEVE evidence, deterministic S7]
       per claim -> grep over src/ via keywords + hints
       script: scripts/retrieve-evidence.sh   (NO LLM)
  -> [Stage 3: JUDGE grounding, adversarial A7]
       per (claim, evidence) -> LLM rules GROUNDED|PARTIAL|CONTRADICTED|UNSUPPORTED
       asset: assets/judge-prompt.md
  -> [Stage 4: SYNTHESIZE]
       aggregate ungrounded -> doc-writer for fix
       re-verify after fix (A8 ALIGNMENT LOOP)
```

Stage 2 is the load-bearing design choice: evidence retrieval is
DETERMINISTIC (grep + AST hints), not LLM. The judge in Stage 3 can
only rule on evidence it actually receives -- it cannot hallucinate
support that the retriever did not find. This is the structural
guard against the failure mode "the LLM convinces itself the docs
match the code."

## Phase 1: SCOPE

Input: list of page paths to verify (1..N). If a `risk_class` is
attached (e.g. "high-stakes"), prefer it; otherwise treat all as equal.

Out-of-scope:
- Pages outside `docs/src/content/docs/` or
  `packages/apm-guide/.apm/skills/apm-usage/`.
- Pages with no factual claims (pure editorial / landing). Skip
  rather than force-extract.

## Phase 2: EXTRACT (parallel)

For each page, dispatch ONE claim-extractor agent:
- Prompt template: `scripts/extract-claims.py <page>` produces the
  prompt and embeds the page content.
- Returns: JSON `{"page", "claims":[{"id","text","section","keywords",
  "expected_source_areas"}]}` capped at 15 claims per page.

Parallel safe; no shared state between extractors.

## Phase 3: RETRIEVE (deterministic, batched)

For each claim, pipe to `scripts/retrieve-evidence.sh`:
- Uses keywords + expected_source_areas to grep src/.
- Returns one-line JSON: `{"claim_id","claim_text","evidence":[...],
  "evidence_count"}`.

Sequential is fine (grep is fast). No LLM. Diagnostics on stderr,
data on stdout.

## Phase 4: JUDGE (parallel)

For each (claim, evidence) tuple, dispatch ONE grounding-judge agent:
- Load `assets/judge-prompt.md`.
- Send the prompt + the tuple.
- Returns: JSON verdict per the schema in `judge-prompt.md`.

Batching across claims-of-one-page into a single judge call is fine
(prompt with all tuples at once). Across pages, fan out.

## Phase 5: SYNTHESIZE

Aggregate verdicts. Materialize the report:

```
{
  "summary": {
    "pages_verified": N,
    "claims_total": N,
    "grounded": N, "partial": N, "contradicted": N, "unsupported": N,
    "grounding_rate": N/total
  },
  "actionable": [
    {"page", "claim", "verdict", "evidence_cited", "fix_suggestion"}
  ]
}
```

CONTRADICTED and PARTIAL are doc-writer work items. UNSUPPORTED is
split: if `retrieval_fix_suggestion` is plausible, retry retrieval
with the suggested keywords; if still empty, treat as CONTRADICTED.

## Phase 6: ALIGNMENT LOOP (A8)

Hand actionable items to doc-writer (one subagent per page). After
edits, RE-RUN the pipeline on the same pages. The grounding_rate
must MONOTONICALLY INCREASE between iterations or the loop has
diverged -- stop and escalate to the operator.

## Ship gate

- grounding_rate >= 0.9 on each verified page after the alignment loop.
- Every CONTRADICTED claim cited a specific code file:line that
  disproves it -- not vague "the code doesn't say that".
- The eval-runner (see `evals/`) passes on the trigger evals and
  the content evals before the skill is treated as production-ready.

## Bundled assets

- `scripts/extract-claims.py` -- Stage 1 prompt builder. `--help`, `--schema`.
- `scripts/retrieve-evidence.sh` -- Stage 2 retriever. Deterministic. `--help`.
- `scripts/verify-page.sh` -- end-to-end orchestrator. `--help`.
- `assets/judge-prompt.md` -- Stage 3 adversarial judge prompt.
- `evals/trigger-evals.json` -- 20 dispatch queries (10 should, 10 shouldn't).
- `evals/content-evals.json` -- seeded-drift recall scenarios.
- `evals/run-evals.sh` -- the eval-runner that turns JSON into metrics.

## Failure modes guarded against

- **Hallucinated grounding**: Stage 2 is deterministic; judge sees only
  real evidence.
- **Adversarial weakness**: Stage 3 prompt defaults to SKEPTICAL.
- **Page-level averaging**: claim-level granularity surfaces partials.
- **Bundle leakage**: design notes / one-time scripts stay in session
  state, never in `references/`.
- **Phantom dependency**: SKILL.md links its persona deps via relative
  paths; A9 PROBE before invoking docs-corpus-audit's substrate.
- **Dispatch collision** with sibling skills: trigger-eval validation
  split is the ship gate (must distinguish from docs-sync /
  docs-corpus-audit triggers).
