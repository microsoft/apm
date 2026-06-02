# CI recovery checklist

Consumed by: `assets/shepherd-driver-prompt.md` (after every push),
`assets/fix-prompt.md` (when a greenfield-fix PR turns red on first
CI run).

Every push from a shepherd-driver or fix subagent MUST be followed
by CI observation. A push that is not observed green is not a
landing candidate.

ASCII only.

## Watch contract

```
gh pr checks <PR> --repo microsoft/apm --watch
```

`--watch` blocks until the check set is conclusive. If `--watch` is
unavailable in the runtime's `gh` version, fall back to polling:

```
while true; do
  out=$(gh pr checks <PR> --repo microsoft/apm)
  echo "$out"
  echo "$out" | grep -qE '(pending|queued|in_progress|running)' || break
  sleep 30
done
```

Settle on one of: ALL GREEN, ANY FAIL, ANY CANCELLED.

## On ALL GREEN

Proceed to the next step in the shepherd-driver loop (Copilot
re-fetch + panel re-run, or final advisory if convergence reached).
Record the green check summary in `ci_evidence`.

## On ANY FAIL or CANCELLED

For each failing check:

```
gh run view <run-id> --repo microsoft/apm --log-failed
```

Classify the failure into one of four buckets:

### Bucket 1 -- lint failure

Symptom: `ruff check` or `ruff format --check` non-silent; pylint
R0801 fires on a duplication threshold; one of the repo's grep
guards (YAML I/O, file length, `relative_to`, auth-signals) fires.

Recovery:
1. Re-run the CI-mirror chain LOCALLY per `.apm/instructions/
   linting.instructions.md`.
2. Auto-fix: `uv run --extra dev ruff check src/ tests/ --fix` and
   `uv run --extra dev ruff format src/ tests/`.
3. Re-run the full chain silent.
4. Commit (one commit per logical fix; ASCII commit message; include
   `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`).
5. Push. Re-enter watch.

### Bucket 2 -- test failure

Symptom: pytest red in the failing job log.

Recovery:
1. Reproduce the failing test locally: `uv run --extra dev pytest -xvs <node-id>`.
2. Read the trace, identify root cause. If the test is asserting on
   new behavior this PR introduces, fix the production code; if the
   test was a pre-existing flake on the test, fix the test only with
   a clear comment.
3. Re-run the test until green.
4. Re-run the broader suite for touched modules.
5. Lint chain silent.
6. Commit + push + re-enter watch.

### Bucket 3 -- CI infra hiccup (transient)

Symptoms: network timeout fetching dependencies, runner pre-empted,
GitHub Actions service disruption, dependency mirror 5xx, action
checkout failure unrelated to the diff. Same job passed minutes ago
on a parent commit.

Recovery:
1. `gh run rerun <run-id> --failed --repo microsoft/apm`.
2. Watch again.
3. Each run-id gets at most ONE re-run. A second failure on the
   same job ID is no longer treated as transient -- escalate to
   Bucket 4.

### Bucket 4 -- persistent unknown failure

Symptom: failure does not match buckets 1-3; same job fails twice;
diff doesn't obviously explain the failure.

Recovery:
1. Record the failing job name, the run-id URL, and a 30-line
   excerpt of the failing log in the shepherd-driver scratch
   context.
2. If the PR's iteration counter for CI recovery is below 3, try
   ONE more fix attempt (e.g. revert the most recent suspect
   commit; re-run). If it succeeds, record both the symptom and the
   fix.
3. If the CI recovery iteration counter hits 3, STOP. Return
   `status: blocked` with the failing job + log excerpt in the
   `blocker` field. Remove `status/shepherding` label. The advisory
   comment names the failing job and points the maintainer at the
   run URL.

## Iteration cap

**Hard cap: 3 CI fix iterations per shepherd-driver run.** Beyond
that the loop terminates with `status: blocked`. The cap covers all
buckets combined (a sequence of lint-then-test-then-infra counts as
three).

## What flows back

The shepherd-driver records in its return:

```json
{
  "ci_iterations": 0..3,
  "ci_evidence": "URL of the final green run, or summary of the
                  failing job for blocked status"
}
```
