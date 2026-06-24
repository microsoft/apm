# apm-issue-autopilot evals

Two eval families: TRIGGER (does the SKILL.md `description:` fire on
the right queries and stay quiet on near-misses?) and CONTENT (does
the skill output structurally differ from the no-skill baseline,
especially at the confidence gate?). Mirrors the batch-bug-shepherd
eval layout; the shared runner is `scripts/run_evals.py` in the
batch-bug-shepherd skill directory.

## Layout

```
evals/
  evals.json                              # manifest + gates + stop_list
  triggers.json                           # 10 fire + 10 no_fire, train/val
  content/
    gate-escalate-doubtful.json           # doubtful accept MUST escalate
    gate-proceed-clean-accept.json        # clean accept MUST auto-proceed
    mixed-types-one-review.json           # ONE consolidated review, B2 router
  fixtures/                               # captured traces (see below)
  results/                                # runner output (gitignored)
```

## Ship gates

- Trigger validation split: should-fire rate >= 0.5 AND should-not-
  fire rate < 0.5 on the `val` split.
- Content: each scenario shows >= 1 rubric anchor present
  `with_skill` and absent (or weaker) `without_skill`. If the two are
  indistinguishable, the skill is not adding value -- redesign.

## Fixtures

The content scenarios reference `fixtures/<id>.with_skill.md` and
`fixtures/<id>.without_skill.md`. These are REAL traces, not
fabricated: capture them by running the scenario `user_query` once
with the skill loaded and once without, then save each transcript to
the named file. Do not hand-author fixtures to make a rubric pass --
that defeats the with/without delta measurement. See
`real-task-refinement.md`.

## Trigger boundary (collision guard)

Autopilot fires on ANY-issue-type, intake-to-merge, triage-central
work over a queue. It must NOT fire on:
- bugs-only queue sweeps or in-flight bug PR shepherding
  -> batch-bug-shepherd;
- single-issue triage -> apm-triage-panel;
- single-PR review -> apm-review-panel;
- PR-body / release-note / docs-edit / issue-creation work
  -> their dedicated skills.
The `no_fire` set encodes each of these siblings.
