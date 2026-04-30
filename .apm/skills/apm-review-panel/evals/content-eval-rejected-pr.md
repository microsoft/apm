# Content eval: PR with architectural smell + nit (expected REJECT)

## Synthetic PR input

- Title: `feat(install): add retry to package downloader`
- Body: Wraps `_download_package` in a 3-attempt retry loop with
  fixed 2-second backoff. Adds a `retries` parameter to
  `InstallPipeline.__init__` defaulting to 3.
- Diff: 35 added lines, 2 removed, 2 files.
- Files:
  - `src/apm_cli/install/pipeline.py` (retry param wiring)
  - `src/apm_cli/deps/github_downloader.py` (retry loop body inline
    in `_download_package`)

## Expected orchestrator behavior

- Spawn 6 panelist tasks in parallel.
- Auth Expert activates (`github_downloader.py` is a fast-path file)
  and returns `active: true` with possibly one finding about token
  refresh on 401.
- Python Architect returns at least one `required` finding:
  `{summary: "Inline retry loop violates Strategy pattern used
  elsewhere in deps/", file: "src/apm_cli/deps/github_downloader.py",
  rationale: "Codebase already has the chain-of-responsibility
  retry pattern in AuthResolver; this should reuse it or extract a
  shared retry helper rather than inlining a fixed loop"}`.
- CLI Logging Expert returns one `nit`: missing `[!]` warning when
  retry kicks in.
- DevX UX Expert returns one `nit`: `retries=3` is undocumented in
  CLI help.
- Supply Chain Security Expert returns 0 findings (no integrity
  surface affected).
- OSS Growth Hacker returns 0 findings.
- CEO synthesizer returns `{arbitration: "Retry behavior is sound
  but the inline loop misses the codebase's existing retry
  abstraction. Python Architect's required action holds.",
  dissent_notes not present}`.
- Orchestrator computes `total_required = 1` -> verdict = REJECT.
- Orchestrator emits ONE comment:
  - Header: `## APM Review Panel Verdict: REJECT`
  - Required section: 1 item, prefixed `[python-architect]`.
  - Nits section: 2 items, prefixed `[cli-logging-expert]` and
    `[devx-ux-expert]`.
  - CEO arbitration paragraph.
  - Per-persona detail in `<details>`.
- Orchestrator applies `panel-rejected` label.
- Orchestrator removes `panel-review` label.

## Pass criteria

- Comment header literally reads `APM Review Panel Verdict: REJECT`.
- Required section has exactly 1 item with `[python-architect]` prefix.
- Verdict label `panel-rejected` is on the PR.
- `panel-review` label is no longer on the PR.
- The auth-expert per-persona block renders (active = true).

## With-skill vs without-skill delta

Same as the clean-PR eval: with skill, the verdict is structured and
machine-actionable (label + binary outcome). Without skill, there is
no schema, no required-vs-nit discipline, and the developer cannot
tell at a glance whether the comment blocks merge or not.
