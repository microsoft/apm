# Architecture invariants - full binding text

Load this file before planning Phase 0. The bullet summaries in
SKILL.md "Architecture invariants" are dispatch anchors; the binding
contract (rationale, edge cases, inherited-from-driver detail) is
here. Every wave honors all of these.

- **Fan-out, not serial.** Triage, strategic-alignment, greenfield
  fix, and per-PR drive all run as parallel child threads via the
  runtime's `task` affordance. A single-loop variant of this skill is
  an anti-pattern -- it collapses the context-isolation win.
- **Verify before fix.** No fix subagent is dispatched until the
  issue is reproduced on HEAD (verdict `LEGIT`). `UNCLEAR` issues
  are surfaced for human triage; `FIXED-AT-HEAD` issues are
  recommended for close.
- **PR-in-flight detection is mandatory.** Before dispatching ANY
  fix, the orchestrator runs `gh pr list --search "<issue-ref>"` (and
  scans linked PRs on the issue) for every legit issue. Skipping
  this step risks duplicating community work, which is the worst
  failure mode this skill defends against.
- **Drive, do not split shepherd from complete.** When a PR exists
  (community in-flight OR own greenfield fix), the entire per-PR
  drive-to-merge loop -- Copilot classification, apm-review-panel,
  fold-vs-defer, push, CI watch, advisory comment -- is owned by ONE
  shepherd-driver subagent (see "Composition with shepherd-driver").
  The orchestrator does NOT run a separate panel wave and a separate
  completion wave; that split lived in earlier versions and is now
  collapsed into the driver loop.
- **Mutation-break gate.** A regression-trap test is REAL only when
  deleting the production guard makes it FAIL. Tests that pass with
  the guard deleted are logic-replay, not regression traps. The
  greenfield FIX subagent runs this gate before opening its PR (see
  `assets/fix-prompt.md`); inside the drive loop shepherd-driver
  re-runs it on every folded regression-trap test.
- **Superseding-PR fallback (inherited from shepherd-driver).** When
  push to the contributor fork fails (no `maintainerCanModify`, branch
  protection, or fork deleted), shepherd-driver opens a new PR under
  `microsoft/apm` that PRESERVES AUTHOR AUTHORSHIP via
  `git commit --author="<author>"` or cherry-pick + `Co-authored-by:`
  trailer, closes the original with a courteous handoff comment, and
  returns `status: superseded`. The orchestrator records it in the
  table; it does not perform the supersede itself.
- **Single-writer interlock per artifact.** Inside the drive loop the
  apm-review-panel posts exactly ONE comment (its own contract) and
  rewrites that same surface idempotently across iterations. Each
  shepherd-driver subagent posts exactly ONE advisory comment at
  terminal. The orchestrator never posts to a PR directly -- it
  delegates to the relevant subagent.
- **ASCII only.** All artifacts (table, comments, commit messages,
  templates) use printable ASCII. No emojis, no em dashes, no
  unicode box-drawing. Windows cp1252 terminals will UnicodeEncodeError
  on anything else.
- **Lint contract is the push gate (inherited).** Before any
  `git push`, the responsible subagent (FIX or shepherd-driver) runs
  the canonical pair:
  `uv run --extra dev ruff check src/ tests/ && uv run --extra dev ruff format --check src/ tests/`
  and both MUST be silent. See `.github/instructions/linting.instructions.md`.
- **Ground-truth table is the single source of truth.** One markdown
  table in the session's plan.md, rewritten on every subagent return.
  Schema in `assets/ground-truth-table.md`. Re-read it at the start
  of every wave (B4 PLAN MEMENTO + B8 ATTENTION ANCHOR).
- **Cross-session message reports only on green.** A shepherd-driver
  (or greenfield FIX) subagent reports back to the orchestrator (via
  the runtime's cross-session-message affordance, or by writing a
  status line to plan.md if cross-session-message is unavailable) ONLY
  when CI is green and all blocking follow-ups landed. Failures stay in
  the subagent's session until resolved or escalated to a human.
- **Operator visibility is a contract, not a courtesy.** At every
  phase boundary the orchestrator MUST render the progress mermaid
  diagram (current phase `active`) + the live ground-truth table
  to chat, AND print a dispatch table immediately before every
  fan-out spawn. The full color contract, render rules, and
  dispatch-table format live in `assets/progress-diagram.md`. Saga
  takes 30+ minutes wall and dozens of parallel subagents; without
  the diagram the operator cannot tell `still working` from
  `stuck`.
- **Mergeability is post-wave truth, not pre-wave assumption.** A
  PR that the drive wave marked ready-to-merge can stop being mergeable
  the moment the maintainer lands another PR onto main. The table
  is not allowed to claim `ready-to-merge` without a post-wave
  `gh pr view --json mergeStateStatus` re-probe. Phase 5 enforces
  this: every ready PR is re-probed; CONFLICTING ones go through a
  one-subagent-per-PR rebase + faithful conflict resolution +
  `--force-with-lease` push + re-probe; non-pushable forks
  (`maintainerCanModify=false`) surface as
  `requires-author-action`. Bare `--force` is prohibited. The gate
  procedure and the conflict-resolution spawn body are inherited from
  shepherd-driver (`../shepherd-driver/references/mergeability-gate.md`,
  `../shepherd-driver/assets/conflict-resolution-prompt.md`).
- **Two-comment-per-PR cap.** Across the entire saga, a single PR
  receives at most TWO orchestrator-controlled comments: the
  shepherd-driver advisory comment (posted at drive-wave terminal),
  and the conflict-resolution resolution-confirmation comment (only
  when Phase 5 resolved a conflict). The in-loop apm-review-panel
  comment is rewritten idempotently by the driver on the same surface
  and does NOT add to the count. No third comment from any phase under
  any circumstance.
- **Bias toward folding recommendations into the PR (inherited from
  shepherd-driver).** Every panel / Copilot follow-up inside the PR's
  stated scope is FOLDED into THAT PR by the driver loop, NOT deferred
  to a tracking issue. shepherd-driver applies the fold-vs-defer rubric
  (`../shepherd-driver/assets/fold-vs-defer-rubric.md`) and biases
  toward FOLD on close calls; only genuinely separable work --
  cross-cutting refactors, broad doc restructuring, new feature work --
  becomes a tracking issue. The driver ships now, not "now plus a
  backlog of papercuts". The orchestrator does not re-run this
  classification; it reads the driver's `folded`/`deferred` return
  fields into the table.
- **Strategic-alignment gate before shepherd work.** After Phase 1
  and BEFORE Phase 2, every LEGIT row passes through Phase 1.5:
  one `apm-ceo` subagent per row inspects the bug against
  `PRINCIPLES.md` (rejection contract) + `MANIFESTO.md`. Rows
  demoted to `out-of-scope` / `wrong-direction` SKIP Phase 2/3/4/5
  and surface in Phase 6 under "Recommend close as out-of-scope".
  The gate FAILS OPEN to `aligned` on subagent malformed-x2 or
  non-citable principle; it ABORTS only when `apm-ceo.agent.md` or
  `PRINCIPLES.md` itself is missing. Silently demoting under
  infrastructure failure would hide real defects. See
  `references/strategic-alignment-gate.md`.

## 17. Worktree isolation

Every mutating child -- each greenfield FIX subagent (Phase 3) and
each shepherd-driver DRIVE subagent (Phase 4), plus any Phase 5
conflict-resolution subagent -- runs in its OWN dedicated git
worktree, one per issue/PR. The orchestrator NEVER fans out mutating
children against a single shared `REPO_ROOT` working tree: parallel
`git checkout`, `gh pr checkout`, commit, rebase, and push operations
would race on `.git/index` and on the single checked-out branch,
corrupting runs or bleeding one PR's changes into another.

Procedure:
- Phase 3 (fix): provision with `git worktree add <path> origin/main`
  before spawning each fix child; pass `<path>` as the child's
  `REPO_ROOT`; record the slug in the row's `worktree` column.
- Phase 4 (drive): own-fix rows REUSE the worktree their Phase 3 fix
  child recorded; community rows provision a fresh worktree
  (`git worktree add` + `gh pr checkout`), recorded in `worktree`.
  The row's `worktree` path is passed as the driver's `REPO_ROOT`.
- Phase 6 (teardown): `git worktree remove` ONLY the slugs recorded
  in the `worktree` column -- never a blanket prune. Leave branches on
  origin for the open PRs.

Triage (Phase 1) and strategic-alignment (Phase 1.5) are READ-ONLY
and MAY share a single read-only `REPO_ROOT`; they never mutate the
tree, so no per-child worktree is required for them.
