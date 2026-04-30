# Content eval: clean PR (expected APPROVE)

## Synthetic PR input

- Title: `docs(readme): clarify install command for fish shell users`
- Body: Adds a one-paragraph note in README.md under the install
  section pointing fish users to the existing `eval (apm shellenv)`
  recipe. No code changes.
- Diff: 4 added lines, 0 removed, 1 file (`README.md`).
- Files: `README.md`

## Expected orchestrator behavior

- Spawn 6 panelist tasks in parallel (5 mandatory + auth-expert with
  `active: false` because no auth files touched).
- Each panelist returns `{persona: "<slug>", required: [], nits: [...]}`
  where `nits` may contain at most a one-line wording suggestion.
- auth-expert returns `{persona: "auth-expert", active: false,
  inactive_reason: "...README.md has no auth surface...", required: [],
  nits: []}`.
- CEO synthesizer returns `{arbitration: "Documentation-only change
  ... all specialists agree no required actions ...", dissent_notes
  not present}`.
- Orchestrator computes `total_required = 0` -> verdict = APPROVE.
- Orchestrator emits ONE comment per `assets/verdict-template.md`:
  - Header: `## APM Review Panel Verdict: APPROVE`
  - Required section: "None."
  - Nits section: rendered (or "None.")
  - CEO arbitration paragraph
  - Per-persona detail in collapsed `<details>` block
- Orchestrator applies `panel-approved` label.
- Orchestrator removes `panel-review` label.

## Pass criteria

- Comment header literally reads `APM Review Panel Verdict: APPROVE`.
- Required section reads "None.".
- Verdict label `panel-approved` is on the PR.
- `panel-review` label is no longer on the PR.
- Comment count from this run is exactly 1.

## With-skill vs without-skill delta

- With skill: structured top-loaded comment, deterministic verdict
  label, fan-out across 6 distinct lenses.
- Without skill: free-form single-voice review with no label
  automation, no schema, no guarantee on which lenses got applied.

The structure IS the value.
