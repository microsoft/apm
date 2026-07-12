# Shepherd Canonical Owner Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent shepherd-driver from returning ready-to-merge without an explicit canonical-owner classification and required dual-guardrail evidence.

**Architecture:** Shepherd-driver owns per-PR enforcement and return evidence. Batch-bug-shepherd inherits the gate, exposes it to greenfield fix agents, and reports it without reimplementing classification.

**Tech Stack:** Agent skill Markdown, JSON Schema, deterministic content evals, Python eval runner, git

---

### Task 1: Extend the completion contract

**Files:**
- Modify: `packages/shepherd-driver/assets/completion-schema.json`
- Sync: `.agents/skills/shepherd-driver/assets/completion-schema.json`

- [ ] **Step 1: Add `architecture_evidence`**

Add this property to `completion_return.properties`:

```json
"architecture_evidence": {
  "type": "object",
  "additionalProperties": false,
  "required": [
    "classification",
    "decisions",
    "dual_guardrail_required",
    "boundary_lint"
  ],
  "properties": {
    "classification": {
      "enum": [
        "ordinary-fix",
        "owner-extension",
        "new-owner",
        "split-authority-repair",
        "not-applicable"
      ]
    },
    "decisions": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["decision", "owner", "consumer_routing"],
        "properties": {
          "decision": { "type": "string", "minLength": 1 },
          "owner": { "type": "string", "minLength": 1 },
          "consumer_routing": { "type": "string", "minLength": 1 }
        }
      }
    },
    "dual_guardrail_required": { "type": "boolean" },
    "behavioral_test": { "type": "string", "minLength": 1 },
    "static_guard": { "type": "string", "minLength": 1 },
    "architecture_test": { "type": "string", "minLength": 1 },
    "mutation_break": { "type": "string", "minLength": 1 },
    "boundary_lint": { "type": "string", "minLength": 1 },
    "rationale": { "type": "string", "minLength": 1 }
  },
  "allOf": [
    {
      "if": {
        "properties": { "dual_guardrail_required": { "const": true } },
        "required": ["dual_guardrail_required"]
      },
      "then": {
        "required": [
          "behavioral_test",
          "static_guard",
          "architecture_test",
          "mutation_break"
        ]
      }
    }
  ]
}
```

- [ ] **Step 2: Require it for ready and advisory terminal returns**

Extend the existing status conditionals so `ready-to-merge` and
`advisory-with-deferred` require `architecture_evidence`.

- [ ] **Step 3: Validate JSON and sync the installed copy**

```bash
python -m json.tool packages/shepherd-driver/assets/completion-schema.json >/dev/null
cp packages/shepherd-driver/assets/completion-schema.json \
  .agents/skills/shepherd-driver/assets/completion-schema.json
cmp packages/shepherd-driver/assets/completion-schema.json \
  .agents/skills/shepherd-driver/assets/completion-schema.json
```

Expected: JSON valid; `cmp` exits 0.

### Task 2: Add the driver gate

**Files:**
- Modify: `packages/shepherd-driver/SKILL.md`
- Modify: `packages/shepherd-driver/assets/shepherd-driver-prompt.md`
- Sync: `.agents/skills/shepherd-driver/SKILL.md`
- Sync: `.agents/skills/shepherd-driver/assets/shepherd-driver-prompt.md`

- [ ] **Step 1: Add Phase X.2.5 to the prompt**

Insert a mandatory step after fold/defer classification:

```markdown
### Step X.2.5 - canonical-owner gate (FAIL CLOSED)

Read `.github/instructions/architecture.instructions.md` and the PR diff.
Classify the PR as exactly one of `ordinary-fix`, `owner-extension`,
`new-owner`, `split-authority-repair`, or `not-applicable`.

For every durable decision touched, record its canonical owner and prove each
consumer routes through that owner. `new-owner` and
`split-authority-repair` always set `dual_guardrail_required=true`.
`owner-extension` sets it true when routing is centralized or repaired.

When dual guardrails are required, do not push or return ready until all four
exist: behavioral regression test, static check in
`scripts/lint-architecture-boundaries.sh`, matching
`tests/integration/test_architecture_*.py` assertion, and mutation-break
evidence. Run `bash scripts/lint-architecture-boundaries.sh` on the exact head.
Missing or uncertain evidence remains in the loop or returns `blocked`; it
cannot be deferred.
```

- [ ] **Step 2: Update terminal criteria and return example**

Add `architecture_evidence` to the documented JSON return and state that
ready-to-merge requires schema-valid evidence.

- [ ] **Step 3: Add the discipline to `SKILL.md`**

Add a named "Canonical-owner gate" discipline and insert the gate into the
convergence loop before lint/push.

- [ ] **Step 4: Sync copies**

```bash
cp packages/shepherd-driver/SKILL.md .agents/skills/shepherd-driver/SKILL.md
cp packages/shepherd-driver/assets/shepherd-driver-prompt.md \
  .agents/skills/shepherd-driver/assets/shepherd-driver-prompt.md
cmp packages/shepherd-driver/SKILL.md .agents/skills/shepherd-driver/SKILL.md
cmp packages/shepherd-driver/assets/shepherd-driver-prompt.md \
  .agents/skills/shepherd-driver/assets/shepherd-driver-prompt.md
```

### Task 3: Propagate visibility through batch-bug-shepherd

**Files:**
- Modify canonical files under:
  `packages/batch-bug-shepherd/.apm/skills/batch-bug-shepherd/`
- Sync matching files under:
  `.agents/skills/batch-bug-shepherd/`

- [ ] **Step 1: Update the binding invariant**

Add to `SKILL.md` and `references/invariants.md`:

```markdown
- **Canonical-owner gate.** Every fix receives an architecture classification.
  A new owner, centralization, or split-authority repair cannot become
  ready-to-merge without behavioral regression, static boundary, matching
  architecture assertion, and mutation-break evidence. Enforcement belongs
  to shepherd-driver; the parent records its return evidence.
```

- [ ] **Step 2: Update the greenfield fix prompt**

After implementation, require the fix subagent to identify durable decisions,
owners, and whether dual guardrails apply. If they apply, require both halves
before opening the PR.

- [ ] **Step 3: Update the final report**

Add under "Disciplines honored this run":

```markdown
- Canonical-owner gate: {{ architecture_gate_count }} PR(s) classified;
  {{ dual_guardrail_count }} authority-affecting fix(es) proved both
  guardrails; {{ architecture_blocked_count }} blocked for missing evidence.
```

- [ ] **Step 4: Sync each changed canonical file**

Copy the changed package files to the same relative paths under `.agents` and
verify each pair with `cmp`.

### Task 4: Add deterministic eval anchors

**Files:**
- Modify both canonical and installed copies of:
  `evals/content/three-issues-mixed.json`
- Modify both canonical and installed copies of:
  `evals/content/sweep-bug-queue.json`
- Modify both `with_skill` fixtures

- [ ] **Step 1: Add rubric anchors**

Add three positive patterns:

```json
{
  "id": "architecture-classification-gate",
  "pattern": "(?is)(canonical[- ]owner|architecture)[^\\n]{0,120}(ordinary-fix|owner-extension|new-owner|split-authority)",
  "weight": 2,
  "description": "Every shepherded PR records an architecture classification."
},
{
  "id": "dual-guardrail-evidence",
  "pattern": "(?is)behavioral[^\\n]{0,100}static[^\\n]{0,100}architecture",
  "weight": 2,
  "description": "Authority-affecting fixes require both guardrail halves."
},
{
  "id": "architecture-fail-closed",
  "pattern": "(?is)(blocked|cannot[^\\n]{0,40}ready)[^\\n]{0,120}(owner|guardrail|architecture)",
  "weight": 2,
  "description": "Missing architecture evidence fails closed."
}
```

- [ ] **Step 2: Update with-skill fixtures**

Add a driver result example containing:

```text
architecture classification: split-authority-repair
behavioral regression: verified by mutation-break
static boundary: scripts/lint-architecture-boundaries.sh
architecture assertion: tests/integration/test_architecture_authorities.py
missing owner evidence cannot return ready-to-merge and is blocked
```

Do not add these anchors to without-skill fixtures.

- [ ] **Step 3: Run evals**

```bash
python packages/batch-bug-shepherd/.apm/skills/batch-bug-shepherd/scripts/run_evals.py \
  --filter content
```

Expected: exit 0 and each with-skill fixture hits all three new anchors.

### Task 5: Validate and open the workflow PR

**Files:**
- Include the committed design and all primitive changes.

- [ ] **Step 1: Run parity, JSON, eval, and repository lint checks**

```bash
python -m json.tool packages/shepherd-driver/assets/completion-schema.json >/dev/null
python packages/batch-bug-shepherd/.apm/skills/batch-bug-shepherd/scripts/run_evals.py
bash scripts/lint-architecture-boundaries.sh
uv run --extra dev ruff check src/ tests/
uv run --extra dev ruff format --check src/ tests/
uv run --extra dev python -m pylint --disable=all --enable=R0801 \
  --min-similarity-lines=10 --fail-on=R0801 src/apm_cli/
bash scripts/lint-auth-signals.sh
```

Expected: all exit 0.

- [ ] **Step 2: Commit**

```bash
git add -f docs/superpowers/specs/2026-07-12-canonical-owner-gate-design.md
git add packages/shepherd-driver .agents/skills/shepherd-driver \
  packages/batch-bug-shepherd/.apm/skills/batch-bug-shepherd \
  .agents/skills/batch-bug-shepherd
git commit -m "feat(shepherd): enforce canonical owner gate" \
  -m "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>" \
  -m "Copilot-Session: 7955c89b-a997-42aa-9c45-ef4c7fe4b1e7"
```

- [ ] **Step 3: Push and open a focused PR**

Use the repository PR-description skill to produce the body, then:

```bash
git push -u origin harden/architecture-owner-gate
```

Open a PR titled `feat(shepherd): enforce canonical owner gate`.
