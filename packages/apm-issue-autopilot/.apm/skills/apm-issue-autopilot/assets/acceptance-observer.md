# Acceptance close (Phase 4, stage 4) - B5 ACCEPTANCE OBSERVER

After the final wave passes its gate, the solution-pipeline child closes
the issue out: it verifies the integrated issue branch satisfies the
`acceptance_shape` from Ideate, then opens the ONE issue PR. This is the
only place a PR is opened in Phase 4 (task children never open PRs).

## Procedure (pipeline child, in the issue worktree)

1. Reload plan.json (B8 ATTENTION ANCHOR). Read `acceptance_shape`.
2. Run the full lint contract one last time (must be silent) and the
   full test suite (`uv run --extra dev pytest -q`, must be green).
3. Verify EACH `acceptance_shape` condition against the integrated
   branch with a deterministic check (run the test, run the command and
   assert the output via a urllib-safe assertion, check the file/link
   exists, confirm the benchmark bound). Do NOT assert satisfaction
   from recall -- every condition is checked with a tool.
4. All conditions hold -> push the issue branch, then author the PR
   body with the `pr-description-skill` dependency (do NOT hand-roll
   the body) and open ONE PR with `gh pr create --body-file`.

   Author the body via the FAST-PATH / INLINE discipline (the `skill`
   tool is usually absent in this pipeline subagent, so INLINE is the
   normal path):
   a. FAST-PATH (if the `skill` tool is present): invoke the
      `pr-description-skill` skill by name. Give it the issue branch,
      base `main`, the integrated diff, the wave commit messages, the
      linked issue (`Closes #N`), the CHANGELOG entry if any, and the
      acceptance-gate evidence (which `acceptance_shape` conditions
      were proven, by which deterministic check). It returns a
      body-file path.
   b. INLINE EXECUTION (normal here): load
      `$REPO_ROOT/.agents/skills/pr-description-skill/SKILL.md` as the
      authoritative contract and follow it IN-THREAD (single-thread
      skill, no fan-out) to write the body file, validating any mermaid
      via its bundled deterministic check.

   Then open the PR (A9 SUPERVISED EXECUTION -- verify the body file
   exists and is non-empty first):

   ```
   test -s "$PR_BODY_FILE" || { echo "empty PR body; cannot open PR"; exit 1; }
   gh pr create --repo microsoft/apm --base main \
      --title "<type>: <short> (closes #N)" --body-file "$PR_BODY_FILE"
   ```

   The body MUST link the issue (`Closes #N`) and summarize the waves
   and the coverage gates proven -- pr-description-skill produces this
   from the inputs above. Return the PR number.
5. A condition fails -> this is a gate-equivalent failure. RE-PLAN from
   the EARLIEST wave whose tasks are responsible for that condition (map
   the failing `acceptance_shape` item to the task/wave whose
   `checkpoint`/`acceptance` covers it; if it cannot be mapped to a
   single wave, re-plan from the last wave). Same `replan_count` <= 2
   cap. If the cap is already spent, return `status: blocked` with the
   failing condition.

## Return to the orchestrator (drop-in compatible with Phase 4)

On success:

```
{ "kind": "implement-result", "issue": <n>, "status": "pr-opened",
  "pr": <num>,
  "coverage_gate": "<aggregate: gates proven across tasks>",
  "plan_ref": "<anchor in plan.md>",
  "waves": <count>, "replans": <replan_count>,
  "routing_receipts": [ <one per child spawned this pipeline; see
    solution-pipeline-prompt.md "Routing receipts"> ] }
```

On terminal failure:

```
{ "kind": "implement-result", "issue": <n>,
  "status": "escalate" | "blocked",
  "reason": "<one paragraph>" }
```

## Hard rules

- The PR is opened ONCE, here, by the pipeline -- never by a task child.
- Do NOT self-merge. Mergeability is Phase 5/6 (shepherd-driver); the
  human approves the protected merge.
- ASCII only. Co-author trailer on the final commit if any close-out
  commit is made.
