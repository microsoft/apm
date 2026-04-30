---
name: apm-review-panel
description: >-
  Use this skill to run a multi-persona expert panel review on a labelled
  pull request in microsoft/apm. The panel fans out to five mandatory
  specialists plus one conditional auth specialist, all running in their
  own agent threads, and a CEO synthesizer. The orchestrator is the sole
  writer to the PR: one comment plus exactly one verdict label
  (panel-approved or panel-rejected, derived deterministically from the
  aggregated findings). Activate when a non-trivial PR needs cross-cutting
  review (architecture, CLI logging, DevX UX, supply-chain security,
  growth/positioning, optionally auth, with CEO arbitration).
---

# APM Review Panel - Fan-Out Expert Review

The panel is FAN-OUT + SYNTHESIZER. Each persona runs in its own agent
thread (via the `task` tool) and returns JSON matching
`assets/panelist-return-schema.json`. The orchestrator schema-validates
each return, hands all returns to the apm-ceo synthesizer (also a task
thread, returns JSON matching `assets/ceo-return-schema.json`), then
deterministically derives the verdict and writes ONE comment plus ONE
verdict label.

## Architecture invariants

- **Verdict is binary and deterministic.** APPROVE iff
  `sum(len(p.required) for p in panelists if p.active) == 0`.
  REJECT otherwise. The CEO does NOT pick the verdict; the CEO writes
  the arbitration narrative. This kills the "approve with reservations"
  failure mode at the schema level.
- **Two severity buckets only.** `required` blocks merge. `nits` are
  one-line suggestions the author can skip. There is no "optional",
  no "consider", no "maybe later". If a finding is real and matters,
  it is required. If not, it is a nit. No third bucket accumulates debt.
- **Single-writer interlock.** Only the orchestrator writes to
  `safe-outputs` (one `add-comment`, one `add-labels`, one
  `remove-labels`). Panelist subagents and the CEO subagent return
  JSON only and MUST NOT call any `gh` write command, post comments,
  apply labels, or touch the PR state.
- **Single-emission discipline.** Exactly one comment per panel run,
  rendered from `assets/verdict-template.md` after all subagents return.

## Agent roster

| Agent | Role | Always active? |
|-------|------|----------------|
| [Python Architect](../../agents/python-architect.agent.md) | Architectural Reviewer | Yes |
| [CLI Logging Expert](../../agents/cli-logging-expert.agent.md) | Output UX Reviewer | Yes |
| [DevX UX Expert](../../agents/devx-ux-expert.agent.md) | Package-Manager UX | Yes |
| [Supply Chain Security Expert](../../agents/supply-chain-security-expert.agent.md) | Threat-Model Reviewer | Yes |
| [OSS Growth Hacker](../../agents/oss-growth-hacker.agent.md) | Adoption Strategist | Yes |
| [Auth Expert](../../agents/auth-expert.agent.md) | Auth / Token Reviewer | Conditional (see below) |
| [APM CEO](../../agents/apm-ceo.agent.md) | Strategic Arbiter / Synthesizer | Yes |

## Topology

```
   apm-review-panel SKILL (orchestrator thread)
                      |
   FAN-OUT via task tool (panelists in parallel)
                      |
   +-----+-------+-------+-----+-----+------+
   v     v       v       v     v     v      v (cond.)
  py    cli     dx-ux   sec   grw           auth
   |     |       |       |     |             |
   |   each returns JSON per panelist-return-schema.json
   +-----+-------+-------+-----+-------------+
                      |
                      v   <-- S4 schema-validate
                      v   <-- on malformed: re-spawn that persona
                      v
   task: apm-ceo synthesizer
   - aggregates required[] across panelists
   - resolves dissent
   - returns ceo-return-schema.json (NO verdict field)
                      |
                      v   <-- DETERMINISTIC verdict gate
                      |       APPROVE iff sum(required) == 0
                      |       REJECT  otherwise
                      v
   orchestrator (sole writer)
            |        |               |
            v        v               v
        add-comment add-labels   remove-labels
        (max:2)     [panel-approved   [panel-review]
                    XOR panel-rejected]
```

## Conditional panelist: Auth Expert

Auth Expert is the only conditional panelist. Activate `auth-expert`
if either rule below matches.

1. **Fast-path file trigger.** Activate the Auth Expert task when the
   PR changes any of:
   - `src/apm_cli/core/auth.py`
   - `src/apm_cli/core/token_manager.py`
   - `src/apm_cli/core/azure_cli.py`
   - `src/apm_cli/deps/github_downloader.py`
   - `src/apm_cli/marketplace/client.py`
   - `src/apm_cli/utils/github_host.py`
   - `src/apm_cli/install/validation.py`
   - `src/apm_cli/deps/registry_proxy.py`

2. **Fallback self-check.** If no fast-path file matched, answer this
   before activating:

   > Does this PR change authentication behavior, token management,
   > credential resolution, host classification used by `AuthResolver`,
   > git or HTTP authorization headers, or remote-host fallback
   > semantics? Answer YES or NO with one sentence citing the file(s).
   > If unsure, answer YES.

Routing rule:

- **YES** -> spawn the auth-expert task with the standard panelist
  contract. It returns JSON with `active: true`.
- **NO**  -> spawn the auth-expert task ANYWAY but instruct it to
  return `{persona: "auth-expert", active: false, inactive_reason:
  "<one sentence citing the touched files>", required: [], nits: []}`.
  This keeps the schema uniform and the verdict template happy.
- Never use wildcard heuristics like `*auth*`, `*token*`, or
  `*credential*` as the sole trigger.

## Routing matrix (emphasis only - all panelists always run)

These routes describe WHICH specialist's findings the CEO weights more
heavily for a given PR type. They do NOT change which personas run -
every mandatory persona runs on every panel invocation, period. Routing
is a CEO synthesis hint, not a panelist gate.

- **Architecture-heavy PR** -> CEO weights Python Architect findings
  on abstraction calls; CLI Logging on consistency.
- **CLI UX PR** -> CEO weights DevX UX on command surface; CLI Logging
  on output paths; Growth Hacker on first-run conversion.
- **Security PR** -> CEO biases toward Supply Chain Security on
  default behavior; DevX UX flags ergonomics regression from any
  mitigation.
- **Auth PR** (auth-expert active) -> CEO weights Auth Expert on
  AuthResolver / token precedence; Supply Chain on token-scoping.
- **Release / comms PR** -> CEO weights Growth Hacker on hook + story
  angle; specialists sanity-check technical claims.
- **Full panel** (default) -> CEO synthesizes equally; calls out any
  dissent in `dissent_notes`.

## Execution checklist

Work through these steps in order. Do not skip ahead. Do not emit any
output to the PR before step 6.

1. **Read PR context** (the orchestrating workflow already fetched it
   via `gh pr view` / `gh pr diff` in step 1 of `pr-review-panel.md`).
   Identify changed files for the auth-expert routing decision.

2. **Resolve the auth-expert conditional** using the rule above.
   Decide: spawn auth-expert active, OR spawn auth-expert with
   `active: false` + an `inactive_reason`. Either way, auth-expert
   IS spawned - the schema requires uniform return shape.

3. **Fan out panelist tasks.** Spawn the following tasks in PARALLEL
   via the `task` tool, one task per persona:
   - `python-architect`
   - `cli-logging-expert`
   - `devx-ux-expert`
   - `supply-chain-security-expert`
   - `oss-growth-hacker`
   - `auth-expert` (always - active per step 2)

   Each task prompt MUST:
   - Reference its persona file by relative path so the subagent loads
     its own scope, lens, and anti-patterns.
   - Include the PR number, title, body, and diff (passed inline).
   - Cite `assets/panelist-return-schema.json` and require the
     subagent to emit JSON matching that schema as its FINAL message.
   - Restate the output contract: NO `gh` write commands, NO posting
     comments, NO label changes, NO touching PR state. JSON return
     only.

4. **S4 schema gate.** When each panelist task returns, parse the JSON
   and validate against `assets/panelist-return-schema.json`. On
   validation failure:
   - Re-spawn that ONE panelist with an explicit error message
     pointing at the violated rule (e.g. "your previous return was
     missing the `nits` field").
   - Maximum two re-spawn attempts per panelist. If still malformed,
     synthesize a placeholder `{persona: "<slug>", active: true,
     required: [], nits: [], extras: {schema_failure: "<reason>"}}`
     and surface the failure in the CEO arbitration prompt.

5. **Spawn the CEO synthesizer task.** Pass the full set of validated
   panelist JSON returns to a `task` invocation that loads
   `../../agents/apm-ceo.agent.md`. The prompt MUST:
   - Provide all panelist returns as structured input.
   - Ask for arbitration prose, dissent resolution, and any growth
     signal worth amplifying.
   - Cite `assets/ceo-return-schema.json` and require JSON return.
   - Restate the contract: CEO does NOT pick verdict, only writes
     arbitration. NO `gh` write commands.

   Validate the CEO return against `assets/ceo-return-schema.json`.
   On failure, re-spawn once with the violation cited.

6. **Compute verdict deterministically:**

   ```
   total_required = sum(len(p["required"]) for p in panelists if p.get("active", True))
   verdict = "APPROVE" if total_required == 0 else "REJECT"
   verdict_label = "panel-approved" if verdict == "APPROVE" else "panel-rejected"
   ```

   The CEO does not influence this calculation. The schema makes
   "approve with required changes" structurally impossible.

7. **Render the comment.** Load `assets/verdict-template.md`, fill the
   placeholders from the panelist + CEO JSON, and emit it as exactly
   ONE comment via `safe-outputs.add-comment`. NEVER call the GitHub
   API directly. NEVER emit per-persona comments or progress comments.

8. **Apply the verdict label** via `safe-outputs.add-labels`:
   `[verdict_label]` (one of `panel-approved` / `panel-rejected`).

9. **Remove the trigger label** via `safe-outputs.remove-labels`:
   `[panel-review]`. This makes the trigger idempotent - re-applying
   the label will re-run the panel cleanly.

## Output contract (non-negotiable)

- Exactly ONE comment per panel run, top-loaded per
  `assets/verdict-template.md`. The `safe-outputs.add-comment.max: 2`
  is a fail-soft ceiling; the discipline lives here.
- Exactly ONE verdict label per panel run (`panel-approved` XOR
  `panel-rejected`).
- Exactly ONE removal of the `panel-review` trigger label.
- Subagents (panelists + CEO) NEVER write to `safe-outputs`, NEVER
  call `gh pr comment`, NEVER call `gh pr edit --add-label`. They
  return JSON. The orchestrator is the sole writer. This discipline
  is mirrored in each persona's "Output contract when invoked by
  apm-review-panel" section.
- Never invent new top-level template sections or drop existing ones.

## Gotchas

- **Roster invariant.** The frontmatter description, the roster table,
  the conditional rule, the verdict template, and the JSON schema
  MUST agree on the persona set. If you change one, change all in the
  same edit.
- **False-negative auth gotcha.** Auth regressions can be introduced
  from non-auth files that change the inputs to auth - host
  classification, dependency parsing, clone URL construction, HTTP
  authorization headers, or call sites that bypass `AuthResolver`. If
  a diff changes how a remote host, org, token source, or fallback
  path is selected and you are not certain it is auth-neutral,
  activate auth-expert as `active: true`.
- **Bundle layout on the runner.** When this skill runs inside the
  PR-review agentic workflow, the APM bundle is unpacked under
  `.github/skills/apm-review-panel/` first, with `.apm/skills/...` as
  a fallback. The asset paths are the same relative to the skill root
  in both layouts.
- **Subagent write enforcement is contract-based, not sandbox-based.**
  In gh-aw, tool permissions are workflow-scoped, not subagent-scoped,
  so every spawned task technically inherits the same `gh` toolset.
  The "subagents must not write" rule is enforced by the prompt
  contract in each `.agent.md` plus the `safe-outputs.add-comment.max:
  2` fail-soft. If a subagent ever tries to post a comment, the cap
  catches it; if it tries to bypass `safe-outputs` and call `gh`
  directly, that is a contract violation worth surfacing in the next
  panel review of the persona file itself.
- **Trigger-label removal is the LAST step.** Doing it earlier risks
  another labeller racing the panel mid-run. The companion workflow
  `pr-panel-label-reset.yml` handles verdict-label invalidation on
  every new push (deterministically, no LLM).
