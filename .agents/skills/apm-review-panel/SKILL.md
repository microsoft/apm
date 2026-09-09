---
name: apm-review-panel
description: >-
  Use this skill to run a multi-persona expert advisory review on a labelled
  pull request in microsoft/apm. The panel fans out to five mandatory
  specialists plus a test-coverage specialist (active on every PR that
  touches src/) plus three conditional specialists (auth, doc-writer,
  performance-expert), all running in their own agent threads, and a CEO
  synthesizer. The orchestrator is the sole writer to the PR: ONE
  recommendation comment, no verdict labels, no merge gating. The panel
  is advisory -- it surfaces findings, prioritizes follow-ups, and renders
  a ship-recommendation that the maintainer and author weigh. Activate
  when a non-trivial PR needs a cross-cutting recommendation
  (architecture, CLI logging, DevX UX, supply-chain security,
  growth/positioning, optionally auth, docs, perf, and test coverage,
  with CEO arbitration).
---

# APM Review Panel - Fan-Out Advisory Review

FAN-OUT + SYNTHESIZER: each persona returns schema-valid JSON in its
own `task` thread; apm-ceo synthesizes in another. The orchestrator
renders ONE advisory comment. The maintainer and PR author decide ship.

## Architecture invariants

- **Advisory, never a gate.** No binary verdict computation, `APPROVE` /
  `REJECT`, or verdict labels. The CEO's `ship_recommendation.stance` (`ship_now`
  / `ship_with_followups` / `needs_discussion` / `needs_rework`); this is
  prose for humans, never a label or status check.
- **Three severity buckets, none of them gate.** Findings carry
  `severity: blocking | recommended | nit`. `blocking` is the highest
  signal a panelist can send and renders prominently in the comment; it
  still does not block merge. `recommended` is the default for substantive
  feedback. `nit` is one-line polish. The orchestrator never reads
  severity to gate anything.
- **Single-writer interlock.** Only the orchestrator emits one comment
  and one label sweep using step 7's transport. Sweep `panel-review`
  (trigger reset), `panel-approved`, and `panel-rejected` (obsolete,
  misleading verdict labels). NO `add-labels`. Panelists and CEO return
  JSON only: no `gh` writes, comments, labels, or PR-state mutations.
- **Single-emission discipline.** Exactly one comment per panel run,
  rendered from `assets/recommendation-template.md` after all subagents
  return.
- **Non-empty turn exit.** In gh-aw, zero safe outputs
  (`agent_output = {"items":[]}`) cause "No Safe Outputs Generated":
  detection is skipped and `add-comment` never runs. Emit the comment
  or, if genuinely impossible, explicit `noop`; never end empty.
  Step 9 scopes verification to the selected transport.
- **Synchronous fan-out -- never spawn-and-forget.** Every panelist and
  CEO `task` MUST use synchronous mode and be awaited for its JSON.
  Never use background/detached mode returning an `agent_id`, or end
  the turn while a child runs: their returns are required for rendering.

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
| [Performance Expert](../../agents/performance-expert.agent.md) | Package-Manager Performance Reviewer | Conditional (see below) |
| [APM CEO](../../agents/apm-ceo.agent.md) | Strategic Arbiter / Synthesizer | Yes |

## Topology

```
orchestrator -> nine parallel panelist tasks -> S4 panelist schema gate
  -> apm-ceo task (aggregate, arbitrate dissent, curate follow-ups)
  -> S4 CEO schema gate -> orchestrator: one comment + one label sweep
```

## Conditional panelists

Three personas are conditional (auth, doc-writer, performance-expert). A
fourth (test-coverage) is mandatory on every PR that touches `src/` and
only skipped on documentation-only PRs -- see its section below for why.
The orchestrator ALWAYS spawns ALL four tasks to keep the schema
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

When active, doc-writer checks changed docs for voice/structure
consistency, code accuracy, completeness (no orphan claims or missing
prerequisites), and discoverability (cross-links, Starlight sidebar
order). Surface missing doc updates required by code changes as findings.

### Performance Expert

Activate when the PR changes any of:
- `src/apm_cli/cache/**`
- `src/apm_cli/deps/**`
- `src/apm_cli/install/phases/**`
- `src/apm_cli/install/pipeline.py`
- `src/apm_cli/install/resolve.py`
- `src/apm_cli/utils/**`
- `src/apm_cli/marketplace/**`
- `src/apm_cli/compilation/**`
- `scripts/perf/**`
- `src/apm_cli/core/command_logger.py` (when the diff adds perf-instrumentation logs)

Also activate when:
- The PR description claims a performance win (speedup ratio, latency
  reduction, bytes-on-disk reduction, throughput improvement) or
  attaches a perf-harness measurement table.
- The diff introduces loops over collections (`for x in collection`)
  where the collection may grow with dependency count or file count.
- The diff adds `os.scandir`, `os.walk`, `os.listdir`, or
  `subprocess.run` calls on a path that executes per-package or
  per-dependency.
- The diff adds `x in list_variable` inside a loop body.

Fallback self-check (when no fast-path file matched): "Does this PR
change the hot path for dependency download, materialization, cache
layout, transport (git protocol, partial clone, sparse checkout),
parallelism, or any user-visible install/update wall-time? Does it
introduce an algorithmic complexity regression (O(n^2) loops, repeated
I/O, missing indexes, unconditional full scans, blocking synchronous
calls, heavy top-level imports)? If unsure, answer YES."

When active, the performance-expert reviews against BOTH:
1. The package-manager performance playbook: transport minimization
   (depth, filter, sparse scope), cache layering and dedup keys,
   parallelism and lock contention, working-tree materialization cost,
   perf-harness methodology (cache wipe, warm/cold separation,
   statistical noise), and pervasive application of the chosen
   technique across install / update / run surfaces.
2. The algorithmic performance lens (Big O analysis): complexity class
   of every loop/lookup in the diff, index vs linear scan patterns,
   unconditional expensive operations, import startup costs, redundant
   computation, and parallelism opportunities. See the agent's
   `references/algorithmic-patterns.md` for the full pattern catalogue.

### Test Coverage Expert

**Active by default on every PR that touches `src/**/*.py`.** The only
condition that flips this persona to `active: false` is a
documentation-only PR -- the diff contains zero `src/**/*.py` files.
In that case set `inactive_reason: "documentation-only PR -- no
runtime code paths to defend"`.

Test evidence (passed / failed / missing) outranks opinion in CEO
arbitration; see `apm-ceo.agent.md` and the panelist schema evidence
block. Never skip this persona heuristically. On a pure refactor,
return a `nit` "no behavior surface touched -- no coverage finding"
rather than leaving the CEO without evidence.

Paired with devx-ux-expert, it defends CLI surface, error wording,
install idempotency, lockfile determinism, and auth resolution.
Verify "no test exists" with `view`/`grep` on the test tree before
reporting. No coverage percentages, pure-refactor test findings, or
duplication of python-architect's test-code design review.

## Routing matrix (CEO synthesis emphasis only)

These synthesis weights NEVER change which personas run.

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

Follow in order; no PR output before step 6. Await every child.
Finish only after comment + label sweep, or step 9's explicit failure/
no-action path.

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
   - `performance-expert` (always - active per step 2)

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
   `../../agents/apm-ceo.agent.md`. Run synchronously and WAIT for its
   JSON before rendering; never detach or end the turn early. Its prompt MUST:
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

   This is the fresh panel pass's only notification mechanism.

7. **Render the comment.** Load `assets/recommendation-template.md`,
   fill the placeholders from the panelist + CEO JSON, and emit it as
   exactly ONE comment.

   **Transport boundary:** Discover the runtime's advertised tools and
   schemas. With gh-aw safeoutputs, invoke structured `add_comment`
   ONCE with the complete markdown directly in its `body` argument.
   No shell staging, wrapper, or intermediate comment file. Configured
   safeoutputs that are missing, failed, or uncertain NEVER authorize
   direct GitHub writes or a second comment. Unknown is not absent.

   Only outside a safe-output workflow, when safeoutputs are absent
   AND the caller authorizes interactive writes, create the body via
   a native file-edit tool, then use
   `gh pr comment <PR_NUMBER> --repo <OWNER/REPO> --body-file <PATH>`.
   Shell text contains only identifiers/path, never final, panelist,
   or CEO prose: no heredocs, `echo`, `printf`, inline scripts,
   substitutions, or encoding workarounds. Without the required tools
   or authority, stop and report explicitly; never bypass the boundary.

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

8. **Sweep labels** once: `[panel-review, panel-approved, panel-rejected]`,
   always all three, even if absent. Use structured `remove_labels` in
   gh-aw (idempotent on missing labels); on step 7's authorized CLI
   path, perform the same cleanup via CLI. Reset the trigger and remove
   obsolete verdict labels; NEVER apply verdict labels.

9. **Verify exit.** In gh-aw, confirm step 7 issued one accepted
   `add_comment` without error; label cleanup alone is not the required
   output. Buffered acceptance is NOT publication: workflow
   post-processing verifies delivery. If all children returned but no
   comment can be produced, call advertised `noop` for intentional
   no-action. If that tool is missing/fails, report failure explicitly,
   never success or a CLI bypass. Zero safe outputs is a FAILURE.
   On the authorized no-safeoutputs CLI path, verify the posted comment
   and label state via CLI read-back; report failures, not fabricated
   delivery or calls to nonexistent safe-output tools.

## Output contract (non-negotiable)

- Exactly ONE comment per panel run, rendered from
  `assets/recommendation-template.md`. The `safe-outputs.add-comment.max:
  2` is a fail-soft ceiling; the discipline lives here.
- Exactly ONE label sweep using step 7's selected transport:
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
- **Calibrated severity.** Distinguish `blocking` from `recommended`;
  CEO arbitration corrects over-flagging, not a merge gate.
- **Mermaid diagrams are template-required.** Request
  `extras.diagrams.class_diagram`, `extras.diagrams.component`, and
  OPTIONAL `extras.diagrams.sequence` from python-architect. Missing
  diagrams remain absent; never invent them.
- **Mermaid `classDiagram` `:::cssClass` shorthand gotcha.** GitHub's
  mermaid renderer rejects `:::cssClass` appended to relationship
  lines (e.g. `A *-- B:::touched`); use standalone
  `class Name:::cssClass` declarations instead. Authority:
  `python-architect.agent.md:146-154`.
- **Doc-writer detects DRIFT, not just edits.** Review consistency
  against the diff, including missing updates, not just touched docs.
- **False-negative auth gotcha.** Auth regressions can be introduced
  from non-auth files that change the inputs to auth -- host
  classification, dependency parsing, clone URL construction, HTTP
  authorization headers, or call sites that bypass `AuthResolver`. If
  a diff changes how a remote host, org, token source, or fallback path
  is selected and you are not certain it is auth-neutral, activate
  auth-expert as `active: true`.
- **Test-coverage probe is mandatory.** The persona verifies missing
  tests via `view`/`grep` on `tests/`; the orchestrator supplies the diff.
- **Subagent write enforcement is contract-based, not sandbox-based.**
  Tool permissions are workflow-scoped, not subagent-scoped, so every
  spawned task technically inherits the same `gh` toolset. The
  "subagents must not write" rule is enforced by the prompt contract in
  each `.agent.md` plus the `safe-outputs.add-comment.max: 2`
  fail-soft. If a subagent ever tries to post a comment, the cap
  catches it.
- **Empty-safe-output failure.** Background spawn-and-forget can end
  gh-aw with no comment. Await every child and follow step 9.
- **Prose is data, not shell source.** PR #1844 documents run
  `27815857237`: the command-safety parser scanned a heredoc and
  rejected a wrapped prose line beginning with `kill`. Quoting does
  not remove this hazard. Preserve words like `kill`, `rm`, `sudo`
  as data; follow step 7, never shell-stage the prose.
- **No verdict-label reset workflow.** The obsolete
  `pr-panel-label-reset.yml` is removed; the advisory regime adds no
  verdict labels.
