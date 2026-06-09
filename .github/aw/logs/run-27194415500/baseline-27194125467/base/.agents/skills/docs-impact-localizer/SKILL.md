---
name: docs-impact-localizer
description: >-
  Use this skill to translate a classifier's in-place verdict into a
  precise, page-by-page work plan for the docs-sync panel. Activate
  after docs-impact-classifier returns verdict in_place; reads the
  candidate page list, fetches the actual page contents, narrows
  scope to specific sections within each page, and emits the
  per-page task brief the panel fans out against.
---

# docs-impact-localizer

Single responsibility: given a list of candidate pages from the
classifier, produce a per-page task brief the docs-sync panel can
fan out against.

You are NOT the verdict-maker (classifier owns that). You are NOT
the writer (doc-writer owns that). You are the **work planner**.

## When to invoke

The docs-sync orchestrator invokes you ONLY when the classifier
returned `verdict: in_place`. For `no_change` you don't run.
For `structural` the architect runs first; you may run after, scoped
to existing pages that need amendment.

## Inputs

- `scope_pages[]` from the classifier
- The PR diff (`gh pr diff $PR`)
- `.apm/docs-index.yml` (per-page metadata)
- Optional: the structural architect's TOC delta (if you run after
  the architect on a structural verdict)

## Step 1: load page contents

For each path in `scope_pages[]`, read the file. Pages are typically
3-10 KB; total budget for this step is bounded by the candidate
count (the classifier should have kept it to <= 6).

## Step 2: narrow scope inside each page

For each page, identify the SPECIFIC section(s) that need to change:

- Read the page's H2/H3 structure
- For each diff symbol from the classifier output, find the section
  most directly documenting it
- Capture line ranges: `lines 120-145` not `the whole page`

The output is a `sections_to_edit[]` per page, where each entry is:

```yaml
page: docs/src/content/docs/consumer/install.md
sections_to_edit:
  - section: "## From Git"
    line_range: [120, 145]
    diff_symbol: "--no-cache flag"
    edit_kind: add | modify | remove
    rationale: "the new --no-cache flag is documented nowhere; section already lists other flags so this is the natural home"
```

## Step 3: detect cross-page conflicts

If two pages document the same symbol and the diff changes the
symbol's behaviour, BOTH pages need an edit AND they must stay
consistent. Flag this in the brief so the CDO synthesizer knows to
cross-check coherence between the two redrafts:

```yaml
cross_page_constraint:
  pages: [path1, path2]
  shared_symbol: "apm install --target"
  consistency_required: "both pages must reflect the same default value"
```

## Step 4: emit the per-page task brief

Return JSON with this shape (one entry per page in `scope_pages[]`):

```json
{
  "tasks": [
    {
      "page": "docs/src/content/docs/consumer/install.md",
      "persona_owner": "consumer",
      "promise": 1,
      "sections_to_edit": [
        {
          "section": "## From Git",
          "line_range": [120, 145],
          "diff_symbol": "--no-cache flag",
          "edit_kind": "add",
          "rationale": "..."
        }
      ],
      "verify_claims": [
        {"claim": "the flag is named --no-cache", "verify_with": "apm install --help"},
        {"claim": "the flag is documented in click.option decorator", "verify_with": "grep -n no-cache src/apm_cli/commands/install.py"}
      ]
    }
  ],
  "cross_page_constraints": [
    {"pages": [...], "shared_symbol": "...", "consistency_required": "..."}
  ],
  "estimated_panel_calls": 8
}
```

The `verify_claims[]` per page is consumed by the python-architect
panelist -- it tells the verifier WHICH claims need a S7 tool-call
check (run `apm install --help`, grep the source) rather than
prose-trusting.

## Output contract

Return a SINGLE JSON document matching the schema in Step 4 as the
final message of your task. No prose around the JSON.

## Anti-patterns

- Selecting whole pages when one section suffices (inflates context per panelist).
- Skipping `verify_claims[]` -- that's the S7 tool-bridge hook; the verifier needs it.
- Inventing pages not in `scope_pages[]` -- that's the classifier's job, not yours. If you think the classifier missed a page, return an extra field `localizer_concern` instead of expanding scope unilaterally.
