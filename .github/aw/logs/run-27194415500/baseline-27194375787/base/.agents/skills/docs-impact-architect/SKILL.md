---
name: docs-impact-architect
description: >-
  Use this skill when the docs-impact-classifier returns a structural
  verdict, signalling that the documentation TOC must change to
  accommodate the PR. Proposes TOC deltas (new pages, moves,
  merges) and emits new-page outline stubs that the doc-sync panel
  later fleshes out. Holds the 3-promise narrative (consume /
  produce / govern) and the persona ramps as hard constraints.
---

# docs-impact-architect

Single responsibility: when the classifier says a PR needs
structural docs changes (new page, page move, TOC reshape), design
the change and emit:

1. A precise TOC delta (added pages, moved pages, retired pages)
2. New-page outline stubs (slug, title, persona, promise, H2 sections, key examples)
3. The persona-ramp impact (which ramp gains/loses a stop)

You are NOT the writer (doc-writer owns prose). You are the **TOC
architect**. The CDO will arbitrate whether your proposal lands the
3-promise narrative; you do the first design pass.

## When to invoke

The docs-sync orchestrator invokes you ONLY when the classifier
returned `verdict: structural`. For `no_change` or `in_place` you
don't run.

## Inputs

- `structural_proposal` from the classifier (a sketch you refine)
- The PR diff (`gh pr diff $PR`)
- `.apm/docs-index.yml` (full corpus map)
- The PR description (for author-stated intent)

## Step 1: read the corpus map, not the corpus

Load `.apm/docs-index.yml` entirely. Inspect `chapters[]`, `pages[]`,
`promises[]`. This is your map. You do NOT read the 100+ page corpus
unless a specific page is implicated by the classifier's sketch.

## Step 2: classify the structural shape

Match the PR's surface change to one of these structural shapes:

| Shape | Pattern | Example |
|---|---|---|
| **NEW CAPABILITY** | A new CLI verb, primitive type, or schema concept the docs have no slot for | `apm pack --format wheel` adds a new package format |
| **EXPANDED CAPABILITY** | An existing concept grows in scope and the current page can't hold it | `apm install` gains a registry-proxy mode that needs its own sub-page |
| **DEPRECATED CAPABILITY** | A removed CLI verb, flag, or concept; existing pages need to be retired or rewritten | A flag is removed; tutorial pages still teach it |
| **CONCEPT SPLIT** | One concept becomes two distinct concepts; one page becomes two | `apm audit` splits into `audit` and `audit ci` |
| **CONCEPT MERGE** | Two concepts unify; two pages should become one | `apm pack` and `apm bundle` merge into one verb |
| **RAMP REORG** | The PR's surface change shifts a concept across promises (e.g. an enterprise feature becomes consumer-default) | Policy enforcement moves from enterprise to consumer default behaviour |

The structural shape drives the TOC delta shape.

## Step 3: design the TOC delta

For each new page proposed, fill in:

```yaml
new_page:
  slug: docs/src/content/docs/<persona>/<topic>.md
  title: "<short imperative title>"
  persona: consumer | producer | enterprise | cross
  promise: 1 | 2 | 3 | cross
  parent_chapter: <existing chapter slug>
  h2_sections:
    - "## Why <topic>"        # OPTIONAL -- skip unless concept is genuinely new
    - "## How to <use>"        # REQUIRED -- code first
    - "## Reference"           # OPTIONAL -- flag/option table
    - "## Troubleshooting"     # OPTIONAL -- only if known footguns
  bridges:
    incoming:                  # which existing pages should link TO this
      - {from: <slug>, link_text: <suggested>}
    outgoing:                  # which existing pages should this link FROM
      - {to: <slug>, link_text: <suggested>}
  ramp_impact: >-
    one-paragraph description of how this changes the <persona>
    ramp: which step it slots into, whether it adds a stop or
    replaces an existing one
```

For each moved/retired page:

```yaml
moved_page:
  from: <slug>
  to: <slug>
  redirect_rationale: <one-sentence>

retired_page:
  slug: <slug>
  reason: <one-sentence>
  redirect_to: <slug>  # MUST exist; orphaning pages breaks SEO
```

## Step 4: validate against the 3-promise narrative

Apply these hard rules. If any fails, redesign:

1. **Every page belongs to exactly one promise.** Cross-cutting pages (integrations, troubleshooting, reference) are explicitly marked `promise: cross`. If a new page straddles two promises, split it OR park it under `cross`.
2. **Consumer pages don't pre-teach producer concepts.** A consumer page may LINK to producer; it may not embed producer prose.
3. **Producer pages don't pre-teach enterprise concepts.** Same rule, one promise down.
4. **No page is orphaned from the TOC.** Every new page has a `parent_chapter` and at least one `incoming` bridge.
5. **No retired page lacks a `redirect_to`.** Search engines will index the old URL for months; the redirect is the SEO contract.

## Step 5: emit the architect report

Return JSON:

```json
{
  "structural_shape": "NEW CAPABILITY" | "EXPANDED CAPABILITY" | "DEPRECATED CAPABILITY" | "CONCEPT SPLIT" | "CONCEPT MERGE" | "RAMP REORG",
  "toc_delta": {
    "new_pages": [...],
    "moved_pages": [...],
    "retired_pages": [...],
    "chapter_changes": [...]
  },
  "promise_validation": {
    "all_pages_single_promise": true | false,
    "no_orphans": true | false,
    "no_unredirected_retires": true | false,
    "concerns": []
  },
  "downstream_in_place_pages": ["..."],
  "rationale": "<2-3 sentence summary of why this structural delta and not alternatives>"
}
```

`downstream_in_place_pages[]` is the handoff to the localizer -- after
the architect approves the TOC, the localizer plans in-place edits
to existing pages that REFERENCE the new structure.

## Output contract

Return a SINGLE JSON document matching the schema in Step 5 as the
final message of your task. No prose around the JSON.

## Anti-patterns

- Inflating new-page counts to seem thorough. The minimal true delta wins.
- Skipping the promise-validation step. The CDO will catch it; better to self-catch.
- Designing a new chapter when an existing chapter has room. Always prefer extending over creating.
- Forgetting `redirect_to` on retired pages. SEO debt is the silent corpus killer.
