---
name: apm-review-panel
description: >-
  Use this skill to run a multi-persona expert advisory review on a labelled
  pull request in microsoft/apm. The panel fans out to five mandatory
  specialists plus a test-coverage specialist (active on every PR that
  touches src/) plus two conditional specialists (auth, doc-writer),
  all running in their own agent threads, and a CEO
  synthesizer. The orchestrator is the sole writer to the PR: ONE
  recommendation comment, no verdict labels, no merge gating. The panel
  is advisory -- it surfaces findings, prioritizes follow-ups, and renders
  a ship-recommendation that the maintainer and author weigh. Activate
  when a non-trivial PR needs a cross-cutting recommendation
  (architecture, CLI logging, DevX UX, supply-chain security,
  growth/positioning, optionally auth, docs, and test coverage, with CEO
  arbitration).
---

# APM Review Panel - Fan-Out Advisory Review

The panel is FAN-OUT + SYNTHESIZER. Each persona runs in its own agent
thread (via the `task` tool) and returns JSON matching
`assets/panelist-return-schema.json`. The orchestrator schema-validates
each return, hands all returns to the apm-ceo synthesizer (also a task
thread, returns JSON matching `assets/ceo-return-schema.json`), then
renders ONE recommendation comment from `assets/recommendation-template.md`.

This skill is ADVISORY by design. It does not compute a binary verdict, it
does not apply verdict labels, and it does not gate merge. The panel
surfaces findings; the maintainer and the PR author decide ship.

## Architecture invariants

- **Advisory regime, not gate regime.** There is no `APPROVE` / `REJECT`,
  no `panel-approved` / `panel-rejected` label, no deterministic verdict
  computation. The CEO returns a `ship_recommendation.stance` (`ship_now`
  / `ship_with_followups` / `needs_discussion` / `needs_rework`); this is
  prose for the human reviewer, never auto-applied as a label or status
  check. This is the architectural fix for the previous regime's
  over-strictness: removing the binary gate removes the incentive for
  panelists to inflate `required[]` defensively.
- **Three severity buckets, none of them gate.** Findings carry
  `severity: blocking | recommended | nit`. `blocking` is the highest
  signal a panelist can send and renders prominently in the comment; it
  still does not block merge. `recommended` is the default for substantive
  feedback. `nit` is one-line polish. The orchestrator never reads
  severity to gate anything.
- **Single-writer interlock.** Only the orchestrator writes to the PR:
  exactly one `add-comment` and one `remove-labels` call. The
  `remove-labels` call always sweeps `panel-review` (trigger
  idempotency) AND defensively removes `panel-approved` /
  `panel-rejected` if present (legacy verdict labels from the
  pre-advisory regime; they have no meaning here and would mislead
  readers if left on a PR after a fresh advisory pass). NO `add-labels`
  call -- there are no verdict labels to apply. Panelist subagents and
  the CEO subagent return JSON only and MUST NOT call any `gh` write
  command, post comments, apply labels, or touch the PR state.
- **Single-emission discipline.** Exactly one comment per panel run,
  rendered from `assets/recommendation-template.md` after all subagents
  return.

## Agent roster

| Agent | Role | Always active? |
|-------|------|----------------|
| [Python Architect](../../agents/python-architect.agent.md) | Architectural Reviewer + supplies mermaid diagrams | Yes |
| [CLI Logging Expert](../../agents/cli-logging-expert.agent.md) | Output UX Reviewer | Yes |
| [DevX UX Expert](../../agents/devx-ux-expert.agent.md) | Package-Manager UX | Yes |
| [Supply Chain Security Expert](../../agents/supply-chain-security-expert.agent.md) | Threat-Model Reviewer | Yes |
| [OSS Growth Hacker](../../agents/oss-growth-hacker.agent.md) | Adoption Strategist | Yes |
| [Auth Expert](../../agents/auth-expert.agent.md) | Auth / Token Reviewer | Conditional (see below) |
| [Doc Writer](../../agents/doc-writer.agent.md) | Documentation Reviewer | Conditional (see below) |
| [Test Coverage Expert](../../agents/test-coverage-expert.agent.md) | Test-Presence Reviewer (paired with DevX UX) | Yes (skipped only on docs-only PRs -- see below) |
| [APM CEO](../../agents/apm-ceo.agent.md) | Strategic Arbiter / Synthesizer | Yes |

## Topology

```
   apm-review-panel SKILL (orchestrator thread)
                      |
   FAN-OUT via task tool (panelists in parallel)
                      |
   +-----+-------+-------+-----+-----+------+-----------+----------+
   v     v       v       v     v     v      v           v          v (cond.)
  py    cli     dx-ux   sec   grw   auth   doc-writer  test-cov
   |     |       |       |     |     |      |           |
   |   each returns JSON per panelist-return-schema.json
   +-----+-------+-------+-----+-----+------+-----------+----------+
                      |
                      v   <-- S4 schema-validate
                      v   <-- on malformed: re-spawn that persona
                      v
   task: apm-ceo synthesizer
   - aggregates findings across panelists
   - resolves dissent
   - emits headline + arbitration prose + principle alignment
   - emits curated recommended_followups (prioritized)
   - emits ship_recommendation (stance + prose)
   - returns ceo-return-schema.json
                      |
                      v   <-- S4 schema-validate
                      v
   orchestrator (sole writer)
            |               |
            v               v
        add-comment    remove-labels
        (max:2)        [panel-review,
                        panel-approved,
                        panel-rejected]
                       (trigger reset +
                        legacy verdict sweep)
```

## Conditional panelists

Two personas are conditional (auth, doc-writer). A third
(test-coverage) is mandatory on every PR that touches `src/` and only
skipped on documentation-only PRs -- see its section below for why.
The orchestrator ALWAYS spawns ALL three tasks to keep the schema
return shape uniform; the prompt instructs the subagent to set
`active: false` with an `inactive_reason` if the condition does not
hold.

### Auth Expert

Activate when the PR changes any of:
- `src/apm_cli/core/auth.py`
- `src/apm_cli/core/token_manager.py`
- `src/apm_cli/core/azure_cli.py`
- `src/apm_cli/deps/github_downloader.py`
- `src/apm_cli/marketplace/client.py`
- `src/apm_cli/utils/github_host.py`
- `src/apm_cli/install/validation.py`
- `src/apm_cli/install/pipeline.py`
- `src/apm_cli/deps/registry_proxy.py`

Fallback self-check (when no fast-path file matched): "Does this PR
change authentication behavior, token management, credential resolution,
host classification used by `AuthResolver`, git or HTTP authorization
headers, or remote-host fallback semantics? If unsure, answer YES."

### Doc Writer

Activate when the PR changes any of:
- `README.md`
- `CHANGELOG.md`
- `MANIFESTO.md`
- `docs/src/content/docs/**`
- `.apm/skills/**/*.md`
- `.apm/agents/**/*.md`
- `.github/skills/**/*.md`
- `.github/agents/**/*.md`
- `.github/instructions/**/*.md`
- `.github/workflows/*.md` (gh-aw natural-language workflows)
- `packages/apm-guide/**`

Fallback self-check (when no fast-path file matched): "Does this PR
change user-facing documentation, agent or skill prose, instruction
files, CHANGELOG entries, README claims, or any natural-language
artifact a reader will rely on? If unsure, answer YES."

When the doc-writer is active and the PR includes documentation changes,
the persona reviews them for: (a) consistency with the existing voice
and structure, (b) accuracy against the code being changed, (c)
completeness for the typical reader (no orphan claims, no missing
prerequisites), (d) discoverability (cross-links, sidebar order if
Starlight content). When the doc-writer is active because of code
changes that SHOULD have updated docs but did not, the persona surfaces
that gap as a finding.

### Test Coverage Expert

**Active by default on every PR that touches `src/**/*.py`.** The only
condition that flips this persona to `active: false` is a
documentation-only PR -- the diff contains zero `src/**/*.py` files.
In that case set `inactive_reason: "documentation-only PR -- no
runtime code paths to defend"`.

The activation rule is intentionally narrow: under the advisory regime,
test outcomes are LOAD-BEARING for CEO arbitration (passed / failed /
missing test evidence outranks opinion-only findings -- see
`apm-ceo.agent.md` and `panelist-return-schema.json` evidence block).
A persona whose findings carry that weight cannot be silently skipped
on a heuristic. Better to spawn it on a pure refactor and have it
return a single `nit`-severity "no behavior surface touched -- no
coverage finding" line than to skip it and leave the CEO without
evidence to weigh. (Earlier revisions of this skill paired test-coverage
with auth and doc-writer as conditional for symmetry; that symmetry
broke when test evidence became load-bearing.)

The test-coverage-expert is paired with the devx-ux-expert lens and
defends the user-promise contracts the DevX persona enumerates (CLI
surface, error wording, install idempotency, lockfile determinism, auth
resolution). It MUST verify "no test exists" claims with `view`/`grep`
on the test tree before emitting a finding -- false-positive coverage
findings destroy trust in the field. It does NOT compute coverage
percentages, does NOT flag tests for pure refactors, and does NOT
duplicate python-architect on test-code design.

## Routing matrix (CEO synthesis emphasis only)

These routes describe WHICH specialist's findings the CEO weights more
heavily for a given PR type. They do NOT change which personas run --
every mandatory persona always runs. Routing is a CEO synthesis hint.

- **Architecture-heavy PR** -> CEO weights Python Architect on
  abstraction calls; CLI Logging on consistency.
- **CLI UX PR** -> CEO weights DevX UX on command surface; CLI Logging
  on output paths; Growth Hacker on first-run conversion.
- **Security PR** -> CEO biases toward Supply Chain Security on default
  behavior; DevX UX flags ergonomics regression from any mitigation.
- **Auth PR** (auth-expert active) -> CEO weights Auth Expert on
  AuthResolver / token precedence; Supply Chain on token-scoping.
- **Docs / release / comms PR** (doc-writer active) -> CEO weights Doc
  Writer on accuracy and voice; Growth Hacker on hook and story angle.
- **Behavior-change PR** (test-coverage active) -> CEO weights Test
  Coverage Expert on regression-trap presence; DevX UX on which user
  promises the change touches. A blocking-severity coverage finding on
  a critical-promise surface (auth, lockfile, install, marketplace,
  hooks) is the highest signal in this routing.
- **Full panel** (default) -> CEO synthesizes equally; calls out any
  dissent in `dissent_notes`.

## Execution checklist

Work through these steps in order. Do not skip ahead. Do not emit any
output to the PR before step 6.

1. **Read PR context** (the orchestrating workflow already fetched it
   via `gh pr view` / `gh pr diff`). Identify changed files for the
   conditional panelist routing decisions (auth-expert and doc-writer).

2. **Resolve the conditional panelists** using the rules above. Decide
   for EACH conditional persona: spawn active OR spawn with
   `active: false` + an `inactive_reason`. Either way, all three
   conditional personas ARE spawned -- the schema requires uniform
   return shape.

3. **Fan out panelist tasks.** Spawn the following tasks in PARALLEL
   via the `task` tool, one task per persona:
   - `python-architect` (also asked to supply `extras.diagrams`:
     `class_diagram` (mermaid `classDiagram`), `component` (mermaid
     `flowchart TD`), and OPTIONAL `sequence` (mermaid
     `sequenceDiagram`) blocks per the persona's section 1/2/3 contract)
   - `cli-logging-expert`
   - `devx-ux-expert`
   - `supply-chain-security-expert`
   - `oss-growth-hacker`
   - `auth-expert` (always - active per step 2)
   - `doc-writer` (always - active per step 2)
   - `test-coverage-expert` (always - active per step 2)

   Each task prompt MUST:
   - Reference its persona file by relative path so the subagent loads
     its own scope, lens, and anti-patterns.
   - Include the PR number, title, body, and diff (passed inline).
   - Cite `assets/panelist-return-schema.json` and require the subagent
     to emit JSON matching that schema as its FINAL message.
   - State the calibrated severity contract: "Use `severity: blocking`
     ONLY for correctness regressions, security/auth bypasses, or
     architectural faults that compound, with explicit rationale.
     Default substantive feedback to `recommended`. Use `nit` for
     one-line polish. The panel is advisory; nothing you return blocks
     merge -- pick the severity that honestly matches your signal
     strength."
   - Restate the output contract: NO `gh` write commands, NO posting
     comments, NO label changes, NO touching PR state. JSON return only.

4. **S4 schema gate.** When each panelist task returns, parse the JSON
   and validate against `assets/panelist-return-schema.json`. On
   validation failure:
   - Re-spawn that ONE panelist with an explicit error message pointing
     at the violated rule.
   - Maximum two re-spawn attempts per panelist. If still malformed,
     synthesize a placeholder
     `{persona: "<slug>", active: true, summary: "Schema failure -- see
     extras.", findings: [], extras: {schema_failure: "<reason>"}}`
     and surface the failure in the CEO arbitration prompt.

5. **Spawn the CEO synthesizer task.** Pass the full set of validated
   panelist JSON returns to a `task` invocation that loads
   `../../agents/apm-ceo.agent.md`. The prompt MUST:
   - Provide all panelist returns as structured input.
   - Ask for: headline, arbitration prose, principle alignment (only
     applicable principles), curated recommended_followups (prioritized
     by signal, NOT a re-listing of every finding), ship_recommendation
     (stance + prose).
   - Cite `assets/ceo-return-schema.json` and require JSON return.
   - Restate the contract: the panel is advisory. The CEO does NOT pick
     a verdict label. The `ship_recommendation.stance` is prose for the
     human reviewer, not a gate. NO `gh` write commands.

   Validate the CEO return against `assets/ceo-return-schema.json`. On
   failure, re-spawn once with the violation cited.

6. **Resolve the notification audience.** The advisory comment must
   surface in the inboxes of the people who will act on it. Run:

   ```
   gh pr view <PR_NUMBER> --json author,reviewRequests
   ```

   Build `notify_audience` as the deduplicated list:
   - the PR author's `@login` (always included);
   - every requested reviewer's `@login` (these are the
     CODEOWNERS-resolved reviewers GitHub auto-requested for the
     touched paths, plus any explicitly-requested human reviewers);
   - every requested team's `@org/team-slug` (CODEOWNERS team
     entries).

   Filter out:
   - bot logins (login ending in `[bot]` or matching
     `dependabot|github-actions|copilot-pull-request-reviewer`);
   - the orchestrator's own identity (avoid self-ping).

   Cap the final list at 6 handles to avoid notification noise (PR
   author + up to 5 reviewers/teams). If the cap trims, prefer team
   handles over individual logins. Pass the resulting list to the
   template renderer as `notify_audience`.

   This step replaces the maintainer-notification signal that the
   pre-advisory verdict labels carried. It is the only mechanism by
   which a fresh panel pass announces itself.

7. **Render the comment.** Load `assets/recommendation-template.md`,
   fill the placeholders from the panelist + CEO JSON, and emit it as
   exactly ONE comment.

   Filling rules:
   - The per-persona summary table renders ONLY active panelists, one
     row per persona, with finding counts by severity and the persona's
     `summary` field.
   - The mermaid diagrams come from `python-architect.extras.diagrams`.
     If absent, render the placeholder lines from the template (do NOT
     invent diagrams).
   - The recommended follow-ups list renders the CEO's curated subset,
     not every finding. Full per-persona findings collapse at the bottom.
   - NEVER render the words "Verdict", "APPROVE", "REJECT", "blocked",
     "merge gate", or any equivalent. The panel is advisory.

8. **Sweep labels** via `safe-outputs.remove-labels`. The list MUST be
   `[panel-review, panel-approved, panel-rejected]` -- always all three,
   regardless of which are currently on the PR. `panel-review` is the
   re-run idempotency reset; the other two are LEGACY VERDICT LABELS
   from the pre-advisory regime that have no meaning under the advisory
   contract and would mislead readers if left on a freshly-reviewed PR.
   `safe-outputs.remove-labels` is idempotent on missing labels, so
   sweeping all three on every run is safe and self-healing. NO
   verdict labels are applied.

## Output contract (non-negotiable)

- Exactly ONE comment per panel run, rendered from
  `assets/recommendation-template.md`. The `safe-outputs.add-comment.max:
  2` is a fail-soft ceiling; the discipline lives here.
- Exactly ONE `remove-labels` call sweeping
  `[panel-review, panel-approved, panel-rejected]`.
- NO `add-labels` call. The advisory regime has no verdict to encode.
- Subagents (panelists + CEO) NEVER write to PR state, NEVER call `gh
  pr comment`, NEVER call `gh pr edit --add-label`. They return JSON.
  The orchestrator is the sole writer.
- Never invent new top-level template sections or drop existing ones.

## Gotchas

- **Roster invariant.** The frontmatter description, the roster table,
  the conditional rules, the recommendation template, and the JSON
  schema MUST agree on the persona set. If you change one, change all
  in the same edit.
- **Calibrated severity discipline.** The advisory regime relies on
  panelists honestly distinguishing `blocking` from `recommended`. If a
  panelist marks everything `blocking`, the comment becomes noisy and
  the maintainer learns to ignore the field. The panelist prompts state
  the contract explicitly; the CEO arbitration prose is the safety
  valve when a panelist over-flags.
- **Mermaid diagrams are template-required.** The python-architect
  persona is asked to supply `extras.diagrams.class_diagram`,
  `extras.diagrams.component`, and the OPTIONAL
  `extras.diagrams.sequence`. The template renders nothing when they
  are missing -- it does NOT invent diagrams. Real diagrams are
  what makes the comment scannable for the human reviewer.
- **Mermaid `classDiagram` `:::cssClass` shorthand gotcha.** GitHub's
  mermaid renderer rejects `:::cssClass` appended to relationship
  lines (e.g. `A *-- B:::touched`); use standalone
  `class Name:::cssClass` declarations instead. Authority:
  `python-architect.agent.md:146-154`.
- **Doc-writer detects DRIFT, not just edits.** When the PR changes
  user-facing code that SHOULD have updated docs but did not, doc-writer
  surfaces that as a finding. The conditional rule above is necessary
  but not sufficient -- doc-writer reasons about doc consistency given
  the diff, not just whether doc files were touched.
- **False-negative auth gotcha.** Auth regressions can be introduced
  from non-auth files that change the inputs to auth -- host
  classification, dependency parsing, clone URL construction, HTTP
  authorization headers, or call sites that bypass `AuthResolver`. If
  a diff changes how a remote host, org, token source, or fallback path
  is selected and you are not certain it is auth-neutral, activate
  auth-expert as `active: true`.
- **Test-coverage probe is mandatory.** The test-coverage-expert MUST
  verify "no test exists for X" via `view`/`grep` on the `tests/` tree
  before emitting a finding. A false-positive coverage finding (test
  exists but persona claimed it does not) destroys maintainer trust in
  the field. The persona scope file enforces this; the orchestrator
  passes the diff and trusts the persona to probe.
- **Subagent write enforcement is contract-based, not sandbox-based.**
  Tool permissions are workflow-scoped, not subagent-scoped, so every
  spawned task technically inherits the same `gh` toolset. The
  "subagents must not write" rule is enforced by the prompt contract in
  each `.agent.md` plus the `safe-outputs.add-comment.max: 2`
  fail-soft. If a subagent ever tries to post a comment, the cap
  catches it.
- **No verdict-label reset workflow.** The previous regime had a
  companion workflow `pr-panel-label-reset.yml` that stripped verdict
  labels on every push. The advisory regime has no verdict labels to
  strip; that workflow is removed.
