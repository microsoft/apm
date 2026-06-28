---
name: adversarial-hardening
description: >-
  Use this skill to systematically harden ONE bounded piece of
  functionality or CLI surface against security and resilience defects
  by looping a red-team plus chaos-engineering panel until a full
  adversarial sweep finds zero new in-scope defects. Activate when the
  maintainer asks to battle-test, red-team, chaos-engineer, stress,
  fuzz, harden, or "try to break" a specific feature or PR and fold
  every fix back in -- even if "red team" is not named. The skill
  ratifies a scope charter FIRST (what the target owns vs what is
  someone else's responsibility), gates every finding against it to
  prevent scope creep, drives each accepted finding to a folded fix
  plus regression trap via shepherd-driver, then reviews its own PR
  with apm-review-panel until ship and authors the body with
  pr-description-skill. Invoke MANUALLY, in-session, on a named
  surface; do NOT use to sweep an issue queue (that is
  apm-issue-autopilot) or to build a domain secret/malware scanner
  (out of scope by design).
---

# adversarial-hardening - red-team / chaos fixpoint orchestrator

This SKILL.md is the natural-language module derived from a genesis
design packet; refactors re-run the genesis skill from that packet.

This skill is a USER-FACING ORCHESTRATOR. The operator invokes it by
name on ONE bounded surface. It COMPOSES three existing siblings and
re-implements none of them:
[shepherd-driver](../shepherd-driver/SKILL.md) for per-finding fold,
[apm-review-panel](../apm-review-panel/SKILL.md) for the terminal
review, and [pr-description-skill](../pr-description-skill/SKILL.md) to
author the PR body. It also bundles two deterministic gates -- a
push-hygiene gate (Pillar B) and a dossier renderer (Pillar A) -- that
keep the run grounded.

## Boundary (what this skill does and does NOT do)

DOES: harden ONE declared surface (a PR #, a module path, or a CLI
command) by looping adversarial lenses to a fixpoint; ratify and gate
against a scope charter; drive each accepted finding to a folded fix
plus regression trap; render a ground-truth findings dossier into the
PR; review the PR to a ship advisory.

Does NOT (these are charter clauses, not preferences):
- Build a domain detector (secret / malware / license scanner). A
  leaked third-party token by shape is the script author's shared
  responsibility, NOT a finding here (charter `OOS-1`). This is the
  EXACT scope creep that consumed ~25 of PR #1798's 32 rounds.
- Reimplement OS / TLS / proxy / kernel facilities; it hardens how the
  TARGET uses them (`OOS-2`).
- Triage an issue queue or open PRs across a backlog -- that is
  apm-issue-autopilot / batch-bug-shepherd (`OOS-3`).
- Remove a legitimate-use capability to close a finding
  (corporate-proxy egress is invariant `INV-1`, not collateral).
- Push unwired or speculative artifacts: uncollected tests, dead
  fixtures, scratch scaffolding are REJECTED at the push-hygiene gate,
  never folded (Pillar B).
- Narrate findings from recall: the findings section is rendered from
  the persisted ledger and embedded verbatim (Pillar A).
- Auto-merge: the protected-branch merge stays a human gate (`PILL-X1`).

## Dependencies and use-site probes (BOTH mechanism)

This skill declares three DIRECT local-path edges in `apm.yml`
(`../shepherd-driver`, `../apm-review-panel`, `../pr-description-skill`)
AND probes each by activation at use-site. Declaration makes the
edge resolvable and audit-visible; the probe fails loud if a sibling
is missing at runtime. Run the matching probe immediately BEFORE you
first compose each sibling:

```
test -f ../shepherd-driver/SKILL.md \
  || echo "[x] MISSING shepherd-driver - stop and ask the operator"
test -f ../apm-review-panel/SKILL.md \
  || echo "[x] MISSING apm-review-panel - stop and ask the operator"
test -f ../pr-description-skill/SKILL.md \
  || echo "[x] MISSING pr-description-skill - stop and ask the operator"
```

If a probe reports MISSING, STOP and ask the operator; do NOT
re-implement the sibling's behavior inline.

## Target set and runtime affordances

Declared target = `common-only`. Use ONLY common-substrate
affordances: sub-agent dispatch (one child thread per lens / per
fold), a persistent plan/FILES store (the findings-ledger lives here),
and a completion signal. Do NOT emit per-harness sugar.

## Bundled assets and scripts

- `assets/scope-charter.template.md` - the anti-scope-creep charter
  template (IN/OUT scope, `OOS-*`, `INV-*`, the five pillar
  invariants). Instantiate ONCE per run.
- `references/lens-catalogue.md` - red-team (`RT-1..RT-5`) + chaos
  (`CH-1..CH-4`) archetypes. Load at the start of each sweep round.
- `references/findings-ledger.md` - the ledger JSON schema, fingerprint
  / dedup rule, fixpoint stop-predicate, dossier column mapping. Load
  before the first round and reload each round (B8 anchor).
- `scripts/check-push-hygiene.sh` - Pillar B gate; JSON on stdout,
  non-zero exit on reject. Run after every fold.
- `scripts/render-dossier.py` - Pillar A renderer; emits the
  `## Hardening findings and resolution` block on stdout. Run once
  before authoring the PR body.

## Orchestrator flow

Maintain the findings-ledger (see `references/findings-ledger.md`) as
the single source of truth throughout. RELOAD the charter and the
ledger at the start of every round and every spawn.

### 0. Charter ratify (B10 human checkpoint)

Instantiate `assets/scope-charter.template.md` for the named surface:
fill IN/OUT scope, the `OOS-*` decline clauses, the `INV-*`
must-not-break invariants, and confirm the five pillar invariants
(`PILL-A1`, `PILL-B1`, `PILL-B2`, `PILL-B3`, `PILL-X1`). Present it to
the operator and do NOT start the loop until they RATIFY. The ratified
charter is the goal steward (B9) for the whole run.

### 1. Alignment loop outer (A8) until fixpoint

Repeat rounds until a full sweep surfaces zero NEW in-scope findings (a
fixpoint PROPERTY -- see the stop-predicate in the ledger reference;
NOT a round counter), or the per-target budget triggers a B10
escalation to the operator (never a silent stop).

### 2. Fan-out the adversarial panel (A1 / B1)

Load `references/lens-catalogue.md`. Spawn ONE read-only child thread
per chosen lens on the current head, each seeded with the lens
charter, the target, and the current fingerprint set (so it skips
known classes -- B13 cost discipline). Pick only the lenses whose
vector-family is plausible; do NOT fan out all nine on a one-flag
surface (A12 gradient). Each lens returns structured JSON findings and
writes nothing.

### 3. Charter-gated arbiter (B9 goal steward)

Synthesize the lens findings into ONE set. For EACH finding:
fingerprint and dedup against the ledger (drop already-seen classes);
gate against the charter. Record the verdict in the ledger:
`accept` (in-scope and new), `decline` (out-of-scope -- record the
declining `OOS-*` clause id in `decline_clause`), or `defer`. An
out-of-scope finding such as "the install path leaks a third-party
token by shape" is DECLINED under `OOS-1`, never folded.

### 4. Reconciliation loop inner (A11) - per-finding fold

The round's accepted findings are a queue. For EACH, drive it to
terminal via shepherd-driver (probe first):

- Compose `../shepherd-driver` to implement the fix plus a
  FAILING-FIRST regression trap, fold it on the head, and confirm CI
  green. shepherd-driver owns the head during its fold, then hands back.
- Run the push-hygiene gate BEFORE accepting the fold:

  ```
  scripts/check-push-hygiene.sh --base <merge-base> --ledger <ledger>
  ```

  It enforces three charter invariants and emits JSON on stdout:
  CI-COLLECTED (`PILL-B1`: every changed test file is collected by the
  merge-queue lane), DIFF-MINIMAL (`PILL-B2`: no orphan fixtures /
  scratch files), RIGHT-ALTITUDE (`PILL-B3`: a cross-module fold
  carries an integration test, not only a unit test). Non-zero exit =
  REJECT.
- On REJECT, the finding does NOT advance to terminal: loop back to
  shepherd-driver to fix the wiring / scope (A11 keeps it open). "Wired
  plus minimal" is structurally a precondition of "fixed".
- On PASS, mark the finding `fixed` in the ledger with `root_cause`,
  `fix_commit`, `trap_path`, `test_kind`, and `ci_collected=true`.

When a round's accepted queue drains, run the next sweep (step 2). When
a full sweep yields zero new in-scope findings, the fixpoint is reached
-- exit the alignment loop.

### 5. Render the dossier (Pillar A, deterministic)

Render the ledger into the findings block:

```
scripts/render-dossier.py --ledger <ledger> > dossier.md
```

This emits a `## Hardening findings and resolution` block: a table of
every finding with each accepted finding's resolution, plus a DECLINED
roll-up (each with its `OOS-*` clause) and a DEFERRED roll-up. The
decline log IN the PR body is itself anti-scope-creep evidence. The
dossier is a PURE FUNCTION of the ledger -- never hand-edit it.

### 6. Author the PR body (pr-description-skill, verbatim embed)

Probe and compose `../pr-description-skill`. Pass it the rendered
dossier block as a MANDATORY embedded section. CONTRACT: it MUST embed
the `## Hardening findings and resolution` block VERBATIM and MUST NOT
paraphrase the findings table; it authors the surrounding narrative
(TL;DR, problem, approach) AROUND it. This preserves the skill's
general role while guaranteeing the findings section is ground-truth.

### 7. Terminal review (apm-review-panel until ship_now)

Probe and compose `../apm-review-panel` on the PR. If the verdict is
not `ship_now`, fold the in-scope panel follow-ups via shepherd-driver
(re-running the push-hygiene gate on each), re-render the dossier, and
re-review. Repeat until `ship_now` or a B10 escalation.

### 8. Hand back (no auto-merge)

Emit the terminal `ship_now` advisory plus the ledger and dossier.
STOP. The operator merges the protected branch by hand (`PILL-X1`).

## One-writer rule

Only the orchestrator writes the PR head, the ledger, and the dossier.
Lenses are read-only recon. shepherd-driver owns the head only during
its fold, then hands back. The push-hygiene gate is a read-only
verifier -- it rejects, it never writes.
