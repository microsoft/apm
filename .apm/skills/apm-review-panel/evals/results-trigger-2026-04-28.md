# Trigger Eval Self-Run (LLM dispatcher = me)

Skill description (verbatim from `.apm/skills/apm-review-panel/SKILL.md`):
> Use this skill to run a multi-persona expert panel review on a labelled
> pull request in microsoft/apm. The panel fans out to five mandatory
> specialists plus one conditional auth specialist, all running in their
> own agent threads, and a CEO synthesizer. The orchestrator is the sole
> writer to the PR: one comment plus exactly one verdict label
> (panel-approved or panel-rejected, derived deterministically from the
> aggregated findings). Activate when a non-trivial PR needs cross-cutting
> review (architecture, CLI logging, DevX UX, supply-chain security,
> growth/positioning, optionally auth, with CEO arbitration).

For each query I record:
- `expected` = label from trigger-evals.json (TRIGGER / NOT)
- `judged`   = my dispatch decision as an LLM reading the description above
- `match`    = expected == judged

## Should-trigger TRAIN (5)

| # | Query | Expected | Judged | Match | Reasoning |
|---|---|---|---|---|---|
| 1 | "review this PR with the expert panel" | TRIGGER | TRIGGER | YES | "expert panel" + "PR" maps cleanly to "multi-persona expert panel review on a labelled pull request" |
| 2 | "run the apm-review-panel against this branch" | TRIGGER | TRIGGER | YES | exact name match |
| 3 | "do a multi-persona review of PR #1234" | TRIGGER | TRIGGER | YES | "multi-persona" is verbatim in description |
| 4 | "panel-review this PR" | TRIGGER | TRIGGER | YES | "panel" + "PR" + uses the trigger-label term as verb |
| 5 | "get the architecture + security + UX review on this PR" | TRIGGER | TRIGGER | YES | enumerates 3 of the 6 listed lenses + PR scope |

Train pass rate: 5/5 = 1.00

## Should-trigger VAL (3)

| # | Query | Expected | Judged | Match | Reasoning |
|---|---|---|---|---|---|
| 6 | "ask the python architect, cli logging expert, and security expert to weigh in on this PR" | TRIGGER | TRIGGER | YES | enumerates 3 of the 5 mandatory specialists by name + PR scope |
| 7 | "have the apm panel verdict this PR" | TRIGGER | TRIGGER | YES | "apm panel" + "verdict" matches "verdict label (panel-approved or panel-rejected)" |
| 8 | "i need a cross-cutting expert review of this change before merge" | TRIGGER | TRIGGER | YES | "cross-cutting" is verbatim in description; "before merge" implies PR scope |

Val pass rate: 3/3 = 1.00 (gate: >= 0.5) -- PASS

## Should-NOT-trigger TRAIN (5)

| # | Query | Expected | Judged | Match | Reasoning |
|---|---|---|---|---|---|
| 9  | "give me a code review of this file" | NOT | NOT | YES | single file, no PR framing, no panel language; would route to a generic code-review skill |
| 10 | "what does the python architect think of this class hierarchy" | NOT | NOT | YES | single persona, not panel; description requires "multi-persona" |
| 11 | "review this docstring" | NOT | NOT | YES | scope is one docstring, not a PR; no panel framing |
| 12 | "fix the lint errors in src/" | NOT | NOT | YES | mechanical task, not review; description is for review only |
| 13 | "what is the apm-review-panel skill" | NOT | NOT | YES | meta-question about the skill, not a request to run it; an info skill should answer this |

Train pass rate (negative): 5/5 = 1.00 (correct rejection)

## Should-NOT-trigger VAL (3)

| # | Query | Expected | Judged | Match | Reasoning |
|---|---|---|---|---|---|
| 14 | "explain how the auth resolver works" | NOT | NOT | YES | explanation request, not review; auth-expert mention is incidental |
| 15 | "summarize the diff of PR #1234" | NOT | NOT | YES | summarization, not multi-persona review with verdict |
| 16 | "draft a PR description for me" | NOT | NOT | YES | authoring, not reviewing; pr-description-skill territory |

Val pass rate (negative): 3/3 = 1.00 (gate: < 0.5 false-positive rate, i.e. >= 0.5 correct rejection) -- PASS

## Summary

| Split | Should-trigger correct | Should-NOT correct | Combined |
|---|---|---|---|
| Train (n=10) | 5/5 = 1.00 | 5/5 = 1.00 | 10/10 = 1.00 |
| Val   (n=6)  | 3/3 = 1.00 | 3/3 = 1.00 | 6/6  = 1.00 |

**SHIP GATE (validation split):**
- Should-trigger pass rate: 1.00 >= 0.5 -- PASS
- Should-NOT pass rate:    1.00 >= 0.5 -- PASS

**RESULT: PASS.** Dispatch description discriminates cleanly with no false positives or false negatives in this run. Caveat: I am a single LLM judging my own routing; the canonical eval would run a different model as dispatcher and average over multiple samples. But this is real LLM judgment, not hand-waving.
