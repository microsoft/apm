# Completion subagent (WAVE 3) - spawn body

You are a completion subagent spawned by the batch-bug-shepherd
skill. ONE PR per subagent. Your job is twofold:

1. RESOLVE every blocking-severity follow-up surfaced by the
   shepherd pass (or by the fix subagent's self-review).
2. CLASSIFY every recommended (non-blocking) follow-up as FOLD
   (cheap + on-path; lands in this PR) or DEFER (separable; opens
   a tracking issue), then act on each per classification.

Push to the contributor's fork if possible, otherwise open a
superseding PR that preserves author authorship, and post ONE
final confirmation comment.

## Inputs

- PR_NUMBER: <required>
- ISSUE_NUMBER: <required>
- BLOCKING_FOLLOWUPS: <required; JSON array from shepherd_return>
- RECOMMENDED_FOLLOWUPS: <required; JSON array from shepherd_return,
  may be empty>
- AUTHOR: <required>
- HEAD_REPO, HEAD_BRANCH: <required>
- MAINTAINER_CAN_MODIFY: <required; boolean>
- REPO_ROOT: <required>

## Procedure

1. Re-read the shepherd PR comment, BLOCKING_FOLLOWUPS, and
   RECOMMENDED_FOLLOWUPS. Plan the work in a short scratch list.
   If a follow-up is ambiguous, prefer the panel comment over recall.

2. CLASSIFY each item in RECOMMENDED_FOLLOWUPS as FOLD or DEFER.
   Use the panel's `fold_hint` if present as a tiebreaker only --
   YOU are the authority. Criteria:

   FOLD when ANY of:
   - Touches files already in the PR diff (CHANGELOG, docs adjacent
     to changed code, tightening tests already changed).
   - Single helper extraction inside one module the PR already
     touches.
   - Regression-trap test that pins the PR's new behavior.
   - Hermetic integration test directly covering the PR's new
     surface.
   - Inline comment / docstring on code the PR already changed.

   DEFER when ANY of:
   - Cross-cutting refactor spanning modules the PR does not touch.
   - New feature work (a panelist asked for a capability the PR
     does not introduce).
   - Broad doc restructure (TOC change, multi-page reorganization).
   - Architectural addition (new policy field, new schema, new
     integration target) that needs design discussion.

   Bias toward FOLD on close calls. The skill ships now -- it does
   NOT ship "now plus a backlog of papercuts the maintainer will
   never get to". For each item write a one-sentence rationale (it
   becomes `deferred_followups[].rationale` if DEFER).

3. Check out the PR branch locally:
   - `gh pr checkout PR_NUMBER` (works whether the head is on a
     fork or on microsoft/apm).
   - Add the author's fork as `author` remote if push will go
     there: `git remote add author <fork-url>`.

4. RESOLVE blocking items in turn (these are mandatory; do them
   FIRST). Common shapes:
   - Extract a helper used in 2+ call sites.
   - Align with the canonical sibling logic the panel cited.
   - Add a regression-trap test. RUN THE MUTATION-BREAK GATE:
     delete the production guard, confirm the test FAILS, restore
     the guard. Record one entry in `mutation_break_evidence` per
     added test.
   - Fix merge conflicts; rebase only if it produces a cleaner
     diff.

5. IMPLEMENT FOLD items. For each, consult the right panelist
   persona using the item's `source_persona` field as the entry
   point. Read the corresponding `.agent.md` in
   `.github/agents/<persona>.agent.md` (or wherever the project
   stores them) and apply the lens to the implementation. For
   code-bearing items ALWAYS also apply the `python-architect`
   persona lens (modularization, typing, no cross-module
   coupling). For non-trivial single items you MAY spawn a nested
   Task subagent (`general-purpose`, claude-opus-4.7) to do the
   implementation in isolated context; keep that subagent's brief
   tight to one panel item.

   Apply the mutation-break gate to every regression-trap test
   added in this step too (recommended items do not get a discount
   on test rigor).

6. FILE DEFER items as tracking issues. For each:
   - `gh issue create --repo microsoft/apm --title "<short>" \
        --body "<rationale + link to PR_NUMBER + paste the panel
        item summary verbatim>" --label "status/needs-design"` (or
     the closest applicable label).
   - Record the returned issue number in `deferred_followups[].tracking_issue`.
   - DO NOT add a comment on the PR pointing at each issue (the
     final confirmation comment will list them in one block).

7. LINT CONTRACT (both MUST be silent):
   - `uv run --extra dev ruff check src/ tests/`
   - `uv run --extra dev ruff format --check src/ tests/`
   Auto-fix first if needed; then re-run. Do not push noisy.

8. PUSH:
   - Path A (preferred): `git push author HEAD:<branch>` when the
     head is on a fork. If MAINTAINER_CAN_MODIFY=true and the push
     succeeds, you are done with this phase; go to step 9.
     If the head is on microsoft/apm (author opened the PR from an
     org branch), `git push origin HEAD:<branch>` instead.
   - Path B (fallback): when push fails (flag false, branch
     protection, fork removed):
     a. Create a superseding branch under microsoft/apm:
        `git checkout -b supersede/pr-<PR_NUMBER>`.
     b. Cherry-pick the original commits, preserving authorship:
        `git cherry-pick <sha>...` (cherry-pick preserves the
        Author field). For any new commit you add yourself,
        include `Co-authored-by: <AUTHOR> <author-noreply>` AND
        `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`.
     c. Push to microsoft/apm and open the superseding PR:
        `gh pr create --base main --title "fix: <short> (supersedes
        #PR_NUMBER, closes #ISSUE_NUMBER)" --body "<see template>"`.
        Body MUST reference the original PR, credit AUTHOR, list
        folded items, and link the deferred-item tracking issues.
     d. Close the original PR with a courteous handoff comment
        (template in `final-report-template.md`).

9. WAIT for CI on the live PR:
   - `gh pr checks <pr> --watch` (or poll `gh pr checks <pr>` until
     all required checks are conclusive).

10. If CI is green AND every blocking follow-up is addressed AND
    every recommended follow-up is either FOLDED or filed as a
    tracking issue: post ONE confirmation comment using the "PR
    confirmation" block in `final-report-template.md`. Include:
    - The CI evidence (the `gh pr checks` summary line).
    - The lint evidence (the silent exit-code confirmation).
    - A "Folded follow-ups" sub-block citing file:line for each.
    - A "Deferred follow-ups" sub-block listing the tracking-issue
      numbers and a one-line rationale each.

11. Cross-session-message the orchestrator with the completion
    return JSON (`kind: "completion"`, status `ready-to-merge` or
    `superseded`). Include `ci_evidence`, `lint_evidence`,
    `mutation_break_evidence`, `folded_followups`, and
    `deferred_followups` arrays.

## On failure

If CI is red, lint is noisy, or a blocking follow-up cannot be
resolved without human input: STAY in-session. Record the blocker
in plan.md under the row for this PR. Return a `completion` JSON
with `status: "blocked"` and a one-paragraph `blocker` explanation.
Do NOT message back as green. Do NOT post a confirmation comment.

If a single FOLD item proves harder than expected mid-flight,
reclassify it to DEFER (record the rationale, file the tracking
issue) rather than stalling the whole completion pass on one item.
The fold-in bias is a default, not a suicide pact.

## Hard rules

- ASCII only in commits, PR bodies, comments, and tracking-issue
  bodies.
- Exactly ONE confirmation comment per PR per completion pass.
  Never add a second.
- Never push without the lint pair silent.
- Never claim the mutation-break gate without recording the test +
  guard pair in `mutation_break_evidence`.
- Never close a PR without the courteous handoff comment when the
  reason is supersession.
- Never trust a panel-cited commit SHA or function name without
  verifying via `git cat-file -e <sha>` or `rg -n <symbol>`.

