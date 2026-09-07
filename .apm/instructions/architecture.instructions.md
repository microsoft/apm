---
applyTo: "src/apm_cli/**"
description: "Single canonical owner discipline: one authority per durable decision, guarded by a regression test + a static boundary check"
---

# Architecture discipline: one canonical owner per decision

## The rule

Every durable decision, vocabulary, outcome, write, or contract has
exactly ONE canonical owner. Every call site routes THROUGH that owner
instead of re-deriving the answer locally.

Adding a second place that computes or enforces the same decision is a split
authority, even when both implementations currently agree.

## Route through the owner

The executable owner inventory lives in
`.apm/architecture/owners/index.json` and its explicitly listed domain shards.
Read that registry before changing a durable decision.

If the owner already exists, route every consumer through it. If it lacks a
case, extend the owner instead of creating a sibling authority. Add a new
registry record only for a genuinely new durable decision.

## Dual guardrail

A centralization or split-authority fix is complete only when it has both:

1. A hermetic behavioral regression test under `tests/`.
2. A registered static guard in the architecture linter, referenced by the
   owner's registry record.

Run the stable CI entrypoint:

```bash
bash scripts/lint-architecture-boundaries.sh
```

When reviewing a change, ask whether it computes or enforces a decision already
owned elsewhere. A new parallel path is a blocking defect, not a nit.
