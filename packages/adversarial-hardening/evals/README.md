# adversarial-hardening evals

This bundle answers two questions deterministically and without
requiring an LLM API key:

1. **TRIGGER EVALS**: does the SKILL.md `description:` reliably match
   real should-fire intents ("red-team the install path",
   "chaos-engineer the runner", "harden the auth module") and avoid
   near-miss queries that belong to sibling skills ("triage the issue
   backlog" -> apm-issue-autopilot, "review this PR" -> apm-review-panel,
   "write a secret scanner" -> out of scope by design)?
2. **CONTENT EVALS**: does loading the SKILL.md body change the shape of
   the run's output, vs not loading it? The scenarios cover the two
   grounding pillars:
   - **Pillar A** (`pillar-a-grounded-findings`): the PR body carries a
     `## Hardening findings and resolution` table rendered from the
     ledger, not a recall summary.
   - **Pillar B** (`pillar-b-push-hygiene`): an uncollected test + stray
     fixture are REJECTED at the push-hygiene gate, not shipped.
   - plus the headline `out-of-scope-decline`: an obvious-but-out-of-scope
     token-leak finding is DECLINED under charter clause OOS-1 instead of
     growing a domain scanner.

## Layout

```
evals/
  evals.json            # top-level manifest (gates, keyword lists)
  triggers.json         # 20 trigger items (10 fire / 10 no-fire),
                        # ~60/40 train/val split
  content/
    out-of-scope-decline.json
    pillar-a-grounded-findings.json
    pillar-b-push-hygiene.json
  fixtures/             # with_skill / without_skill markdown pairs
  results/              # timestamped run output (gitignored)
```

## Matcher

The trigger matcher (see `scripts/run_evals.py`) approximates a
dispatcher deterministically: a `stop_list` negative override fires
first (near-miss queries that belong to siblings), then verbatim
`trigger_keywords_primary` phrases, then a secondary-threshold path
requiring >= 3 `trigger_keywords_secondary` tokens AND at least one
`secondary_anchor_tokens` token (so a pile of generic words cannot fire
without a domain-specific anchor like `exploit`/`break`/`adversarial`).

## Run

From the worktree root:

```
python packages/adversarial-hardening/scripts/run_evals.py
```

Or against the deployed copy:

```
python .agents/skills/adversarial-hardening/scripts/run_evals.py
```

Exit `0` = all gates met, `1` = a gate failed, `2` = runner error.
The `val` split is the ship gate (>= 0.5 should-fire, < 0.5 near-miss
miss-rate); every content scenario must show `delta_anchors >= 1`.
