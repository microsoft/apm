# findings-ledger - state table schema (Pillar A source of truth)

The findings-ledger is the SINGLE SOURCE OF TRUTH for a hardening run.
Every adversarial finding the lenses surface, every charter verdict
the arbiter renders, and every resolution shepherd-driver folds is
recorded here FIRST, then read back. The hardening-dossier in the PR
body is a pure deterministic FUNCTION of this ledger
(`scripts/render-dossier.py`) -- never an LLM summary from recall
(Pillar A, design Step 3.6).

The ledger is a B4 PLAN MEMENTO: persist it to the runtime FILES slot
(e.g. the session plan store) so it survives context degradation, and
RELOAD it at the start of every round, every lens spawn, and every
arbiter pass (B8 ATTENTION ANCHOR).

## Storage format

A single JSON document at the run's ledger path (the orchestrator
chooses the path; pass it to both bundled scripts via `--ledger`).
ASCII only. Shape:

```json
{
  "schema_version": 1,
  "target": "PR #1529 lifecycle-scripts runner",
  "charter_path": "assets/scope-charter.instance.md",
  "round": 3,
  "findings": [
    {
      "id": "F-003",
      "round": 1,
      "lens": "resource-exhaustion",
      "fingerprint": "env-parse:unbounded-recursion:cli.runner",
      "severity": "high",
      "charter_verdict": "accept",
      "decline_clause": null,
      "status": "fixed",
      "root_cause": "runner recursed on $-expansion with no depth cap",
      "fix_commit": "a1b2c3d",
      "trap_path": "tests/unit/test_runner_expansion.py::test_depth_cap",
      "test_kind": "unit",
      "ci_collected": true
    }
  ]
}
```

## Column reference

| Field | Type | Meaning |
|-------|------|---------|
| `id` | string | Stable finding id, `F-NNN`, assigned on first sighting. |
| `round` | int | Sweep round in which the finding was FIRST surfaced. |
| `lens` | string | Archetype that surfaced it (see lens-catalogue). |
| `fingerprint` | string | Dedup key (see below). One per vulnerability CLASS. |
| `severity` | enum | `low` / `medium` / `high` / `critical`. |
| `charter_verdict` | enum | `accept` / `decline` / `defer` (arbiter, charter-gated). |
| `decline_clause` | string\|null | Charter clause id that DECLINED it (out-of-scope). Required when verdict=decline. |
| `status` | enum | `open` / `fixed` / `declined` / `deferred`. |
| `root_cause` | string\|null | One-line cause (accepted findings, post-fix). |
| `fix_commit` | string\|null | Short SHA of the fold (accepted+fixed). |
| `trap_path` | string\|null | Regression-trap test id proving the fix. |
| `test_kind` | enum\|null | `unit` / `integration` (altitude; see Pillar B). |
| `ci_collected` | bool\|null | True once push-hygiene confirms the trap is collected by the merge-queue lane. |

## Fingerprint / dedup rule (kills the 32-round re-discovery cost)

The `fingerprint` is a stable, lowercase, colon-delimited key naming
the vulnerability CLASS, NOT the instance:

```
<vector-family>:<mechanism>:<surface>
```

e.g. `input-boundary:argv-injection:cli.install`,
`fault-injection:partial-write:lockfile.writer`.

Dedup contract, applied by the charter-arbiter BEFORE accepting:
1. Normalize the candidate fingerprint (lowercase, collapse spaces).
2. If an OPEN or FIXED ledger row already carries that fingerprint,
   the candidate is a DUPLICATE -> do NOT create a new row, do NOT
   re-pay an implementer dispatch. Annotate the existing row's round
   list if useful.
3. Only a genuinely NEW fingerprint becomes a new `F-NNN` row.

A lens MUST receive the current fingerprint set in its task context so
it can skip known classes (B13 cache-aware: the fingerprint set is
part of the variable suffix; the charter + target are the stable
prefix).

## Stop-predicate (fixpoint -- a PROPERTY, not a round counter)

The outer A8 alignment loop CONVERGES when, and only when:

> a full adversarial sweep (every lens, fresh on the current head)
> surfaces ZERO new in-scope fingerprints AND the ledger has no row
> with `status = open`.

"In-scope" means the arbiter would `accept` it under the charter; an
out-of-scope discovery does NOT keep the loop alive (it is recorded
`declined` and the sweep still counts as a zero-new sweep). This is
what prevents the secret-scanner class from looping forever.

Termination is NEVER a round cap. The diminishing-returns DAMPER
(design Step 3.2 item 5) is the only spend governor: when a full sweep
yields only `low`-severity or duplicate findings, ESCALATE to a B10
human checkpoint ("marginal hardening; spend more?") rather than
auto-continue or auto-stop.

## Dossier column mapping (Pillar A -- every dossier cell maps to a ledger cell)

`scripts/render-dossier.py` reads this ledger and emits the
`## Hardening findings and resolution` block. The mapping is total --
no dossier value is computed from anything but a ledger field:

| Dossier section | Ledger source |
|-----------------|---------------|
| Findings table: ID | `id` |
| Findings table: Lens | `lens` |
| Findings table: Severity | `severity` |
| Findings table: Verdict | `charter_verdict` |
| Findings table: Resolution (root cause) | `root_cause` |
| Findings table: Fix commit | `fix_commit` |
| Findings table: Regression trap | `trap_path` (+ `test_kind`) |
| Declined roll-up | rows where `charter_verdict=decline`, keyed by `decline_clause` |
| Deferred roll-up | rows where `status=deferred` |

Because the render is a pure function of the ledger, the PR's findings
story cannot drift from what was actually found and fixed.
