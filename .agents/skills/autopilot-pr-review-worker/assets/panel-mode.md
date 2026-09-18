# panel-mode contract

Load-bearing for `autopilot-pr-review-worker`. Honor this file
together with SKILL.md. Unknown or omitted `panel-mode` -> `lean`
(fail cheap, not fail heavy). Not a missing-field stop.

## Modes

| Mode | When | Roster | Context | Comment |
|------|------|--------|---------|---------|
| full | First advisory on a PR, or a human asks | Surface-gated specialists + CEO | Shared brief + owned files | yes |
| lean | Small / obvious surface, or cost cap | Core 2-3 + CEO | Shared brief only | yes |
| delta | Later merge-worker iteration, terminal | CEO + personas whose files changed since last panel | Brief + last comment + diff | yes if head or watermark changed; else noop |

`full` is never "spawn everyone including inactive stubs."
Even `full` is surface-gated.

## Core (always, unless docs-only)

- `python-architect` -- skip only on docs/changelog-only
- `test-coverage-expert` -- skip only on docs-only (`src/` untouched)
- `apm-ceo` -- always

## Surface-gated (spawn only on path match)

Do NOT spawn `active: false` stubs to keep a uniform shape.
Omitted persona = inactive. Template already skips inactive rows.

| Persona | Fast-path |
|---------|-----------|
| auth-expert | `src/apm_cli/core/auth.py`, `token_manager.py`, `azure_cli.py`, `github_downloader.py`, `marketplace/client.py`, `utils/github_host.py`, `install/validation.py`, `install/pipeline.py`, `deps/registry_proxy.py` |
| doc-writer | `docs/`, `CHANGELOG.md`, `README.md`, `MANIFESTO.md`, `.apm/` skills/agents, `.github/` skills/agents/instructions, `packages/apm-guide/` |
| performance-expert | `cache/`, `deps/`, `install/phases/`, `install/pipeline.py`, `install/resolve.py`, `utils/`, `marketplace/`, `compilation/`, `scripts/perf/` |
| cli-logging-expert | `console.py`, CommandLogger, STATUS_SYMBOLS |
| devx-ux-expert | CLI surface, help, init/install/run |
| supply-chain-security-expert | lockfile, downloaders, integrity, tokens |
| oss-growth-hacker | README, MANIFESTO, CHANGELOG positioning, first-run docs |

Fast-path is the default include set, not the ceiling.

After packing the shared brief, the orchestrator MUST think: for
each surface-gated persona the fast-path missed, does this PR's
intent, conversation, or hidden coupling still need that lens?

- Add the persona when you can write a one-line `adhoc_reason`
  (example: "host classification feeds AuthResolver even though
  auth.py is untouched").
- Skip when you cannot. Do not add "to be sure" or "to keep the
  schema uniform."
- Record `adhoc_reason` next to the slug in `personas_spawned`.
- `lean` still prefers the cap; an ad-hoc add is allowed when
  the reason is stronger than the cap.
- `delta` may add a persona that was inactive on the prior
  panel only with an `adhoc_reason` or a new fast-path file.

Fallback self-check questions are prompts for that thinking.
They are not a hard "at most one" quota.

## Lean cap

1. the single highest-signal surface owner
2. test-coverage if `src/` touched
3. CEO

Architect is optional in `lean` when the diff is under 3 production
files and there is no new module / owner split.

## Delta cap

1. CEO (required)
2. each previously-active persona whose owned files appear in
   `git diff <last_panel_head> HEAD`
3. test-coverage only if tests or `src/` changed in that range

No new persona that was inactive on the prior panel, unless a new
fast-path file appeared or the orchestrator writes an
`adhoc_reason`.

## Shared brief

Orchestrator gathers once. Children MUST NOT re-fetch the PR
conversation or walk the repo "to be sure." Packet target < 8 KB.
Example: `assets/shared-brief.example.json`.

Prompt contract: "JSON only. Do not run gh. Do not read files
outside this packet unless a path is listed as owned by you."

Architect mermaid: `full` only. `lean` / `delta` omit diagrams.
Finding cap: 3 per persona. Nits allowed only in `full`.

## Models

Pin so a missing default cannot retry the whole roster.

| Role | Class |
|------|-------|
| CEO, auth, supply-chain, architect | high-capability |
| test-coverage, doc-writer, logging, devx, growth, perf | cheap/fast |

If a pinned model is unavailable: fall back **once** per persona
to the other class, then stub that persona with `active: false` and
`inactive_reason: model unavailable`. Do not relaunch the roster.

## Merge-worker composition

```
iteration 1:  panel-mode=full   (or lean if the diff is tiny)
iteration 2+: panel-mode=delta
terminal:     panel-mode=delta  (noop if head+watermark unchanged)
```

Hard cap: one `full` per merge-worker run. A second `full` needs
`panel_escalation` on the completion_return.
