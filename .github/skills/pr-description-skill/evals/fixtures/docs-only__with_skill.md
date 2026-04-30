docs(quickstart): add 5-minute getting-started guide

## TL;DR

New contributors land on the docs index, scroll, and bounce.
This PR adds a 5-minute quickstart linked from the landing page
so first-touch users have a happy path before reading reference
material.

## Problem (WHY)

- Issue #640 reports a 60% bounce rate on `/docs/` for new
  visitors.
- The current landing page lists every guide in alphabetical
  order; "Quickstart" did not exist.
- Agent Skills makes the diagnosis explicit:
  ["agents pattern-match well against concrete structures"](https://agentskills.io/skill-creation/best-practices)
  -- and the same is true for human readers landing cold.

## Approach (WHAT)

Additive: see Implementation. One new page, one link added on
the index.

## Implementation (HOW)

| File | Change |
|---|---|
| `docs/src/content/docs/guides/quickstart.md` | NEW. 5-minute happy path: install, init, run. |
| `docs/src/content/docs/index.md` | Promote quickstart link above the alphabetical guide list. |

## Trade-offs

- **Promoting one guide above the list**: minor visual
  asymmetry; pays for itself if bounce rate drops.
- **Rejected**: rewriting the landing page. Higher risk, larger
  diff, harder to A/B.

## Benefits

1. Closes #640 if bounce rate drops measurably.
2. New contributor first-touch falls under 5 minutes.
3. Zero code change; rollback is `git revert`.

## Validation

```
$ npm run --prefix docs build
built in 4.2s
no broken links
$ apm audit --ci
0 findings
```

## How to test

- [ ] `npm run --prefix docs dev`
- [ ] Open `http://localhost:4321/docs/`; confirm Quickstart
      link is above the fold.
- [ ] Click through; confirm the 5 steps complete on a fresh
      clone in under 5 minutes.

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
