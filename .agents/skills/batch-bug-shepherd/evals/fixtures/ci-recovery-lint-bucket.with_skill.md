# Shepherd-driver subagent trace: WITH batch-bug-shepherd skill loaded

Input fixture: shepherd-driver Phase X.5 just pushed iteration 2 of
PR #1490. Phase X.6 enters CI watch.

## Phase X.6 -- CI watch + recovery

Watch contract per `assets/ci-recovery-checklist.md`:

```
gh pr checks 1490 --repo microsoft/apm --watch
```

Settled on ANY FAIL. One failing job: `Lint`.

Inspecting the failing job log (do NOT guess from the surface check
name):

```
gh run view 7104812345 --repo microsoft/apm --log-failed
```

Excerpt:

```
src/apm_cli/utils/console.py:142:101: E501 Line too long (108 > 100)
Found 1 error.
```

## Classify the failure

Per `assets/ci-recovery-checklist.md`:

- Bucket 1 -- lint failure (ruff check / format / R0801 / repo
  guards / auth-signals)
- Bucket 2 -- test failure
- Bucket 3 -- CI infra hiccup (transient)
- Bucket 4 -- toolchain drift

Symptom is `ruff check` E501 non-silent on a touched file ->
**bucket 1 (lint failure)**.

## Recovery

Step 1: re-run the CI-mirror chain LOCALLY per
`.apm/instructions/linting.instructions.md`:

```
uv run --extra dev ruff check src/ tests/
uv run --extra dev ruff format --check src/ tests/
```

Confirmed: the format-check fires on the same line.

Step 2: auto-fix:

```
uv run --extra dev ruff check src/ tests/ --fix
uv run --extra dev ruff format src/ tests/
```

Step 3: re-run the full chain silent:

```
uv run --extra dev ruff check src/ tests/ \
  && uv run --extra dev ruff format --check src/ tests/ \
  && uv run --extra dev python -m pylint --disable=all --enable=R0801 \
     --min-similarity-lines=10 --fail-on=R0801 src/apm_cli/ \
  && bash scripts/lint-auth-signals.sh
```

All four steps silent. Lint chain re-verified silent locally before
push.

Step 4: commit (one logical fix; ASCII commit message; Copilot
trailer):

```
git add src/apm_cli/utils/console.py
git commit -m "style(console): wrap long line in color helper (ruff E501)

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

Step 5: push. Re-enter watch.

```
git push origin feat/no-color-flag
gh pr checks 1490 --repo microsoft/apm --watch
```

## Counter and loop continuation

ci_iterations advanced: 1 of 3 cap. Loop is NOT terminal; on
re-watch ALL GREEN, the shepherd-driver proceeds to Phase X.7
(decide terminal vs next outer iteration). On re-watch ANY FAIL,
re-enter Phase X.6 with ci_iterations++; if cap (3) hit, terminal
return `blocked` with the failing job + log excerpt in the
completion-return payload.

Return-shape excerpt for the orchestrator (not yet sent; mid-loop):

```json
{
  "ci_iterations": 1,
  "ci_bucket_history": ["lint"],
  "ci_recovery_applied": "ruff format src/ tests/"
}
```
