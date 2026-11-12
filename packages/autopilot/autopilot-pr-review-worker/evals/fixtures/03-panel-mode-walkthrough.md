# panel-mode walkthrough (auth-sized, docs-only, CLI help-only)

Specification fixture. Not a live PR. Orchestrators use this to
pick roster and context without spawning inactive stubs.

## Auth-sized (like PR #2610)

Touched: `src/apm_cli/core/auth.py`, token/host files, tests under
`tests/`.

| Mode | Spawn | Context | Diagrams | Nits |
|------|-------|---------|----------|------|
| full | auth-expert, python-architect, test-coverage-expert, supply-chain-security-expert (token surface), apm-ceo | shared brief + owned files | yes | allowed |
| lean | auth-expert, test-coverage-expert, apm-ceo | shared brief only | no | no |
| delta | CEO + prior active personas whose files changed since last panel head | brief + last comment + diff | no | no |

Do not spawn cli-logging, growth, or doc-writer stubs.
Orchestrator may still add one of those with an `adhoc_reason`
if the conversation or hidden coupling needs that lens
(example: README conversion copy on an auth PR).

## Docs-only

Touched: `docs/src/content/docs/**` and/or `CHANGELOG.md`. Zero
`src/**/*.py`.

| Mode | Spawn | Notes |
|------|-------|-------|
| full | doc-writer, oss-growth-hacker (if README/CHANGELOG positioning), apm-ceo | skip python-architect and test-coverage |
| lean | doc-writer, apm-ceo | highest-signal surface owner is doc-writer |
| delta | CEO + doc-writer if docs changed in range | noop if head+watermark unchanged |

## CLI help-only

Touched: CLI help strings / `src/apm_cli/cli.py` help text, no new
module, fewer than 3 production files.

| Mode | Spawn | Notes |
|------|-------|-------|
| full | devx-ux-expert, cli-logging-expert, python-architect, test-coverage-expert, apm-ceo | surface-gated, not the old 9-way stub roster |
| lean | devx-ux-expert, test-coverage-expert, apm-ceo | architect optional (tiny diff, no owner split) |
| delta | CEO + personas whose owned files changed | merge-worker iteration 2+ and terminal |

## Merge-worker composition

```
iteration 1:  panel-mode=full   (or lean if the diff is tiny)
iteration 2+: panel-mode=delta
terminal:     panel-mode=delta  (noop if head+watermark unchanged)
```

Hard cap: one `full` per merge-worker run. A second `full` needs
`panel_escalation` on the completion_return.
