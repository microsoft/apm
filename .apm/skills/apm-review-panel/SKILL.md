---
name: apm-review-panel
description: >-
  Use this skill to run a six-agent panel review (plus one conditional
  auth specialist) for non-trivial PRs, design proposals, release
  decisions, and other cross-cutting changes in microsoft/apm. Emit one
  synthesized verdict comment.
---

# APM Review Panel -- Expert Review Orchestration

The panel is fixed at **5 mandatory specialist lenses + 1 conditional
auth lens + 1 arbiter lens = up to 7 persona sections in one verdict
comment**. You play each lens in turn from inside a single agent loop
(progressive-disclosure skill model -- no sub-agent dispatch). Routing
chooses *which* lenses execute; it never changes which headings appear
in the final verdict.

## Agent roster

| Agent | Persona | Always active? |
|-------|---------|----------------|
| [Python Architect](../../agents/python-architect.agent.md) | Architectural Reviewer | Yes |
| [CLI Logging Expert](../../agents/cli-logging-expert.agent.md) | Output UX Reviewer | Yes |
| [DevX UX Expert](../../agents/devx-ux-expert.agent.md) | Package-Manager UX | Yes |
| [Supply Chain Security Expert](../../agents/supply-chain-security-expert.agent.md) | Threat-Model Reviewer | Yes |
| [OSS Growth Hacker](../../agents/oss-growth-hacker.agent.md) | Adoption Strategist | Yes (side-channel to CEO) |
| [Auth Expert](../../agents/auth-expert.agent.md) | Auth / Token Reviewer | Conditional (see "Conditional panelist" below) |
| [APM CEO](../../agents/apm-ceo.agent.md) | Strategic Owner / Arbiter | Yes (always arbitrates) |

## Routing topology

```
  python-architect    cli-logging-expert    devx-ux-expert    supply-chain-security-expert
        \_______________________|______________________________/
                                |   <-- auth-expert (conditional)
                                v
                            apm-ceo               <----  oss-growth-hacker
                       (final call / arbiter)           (annotates findings;
                                                         updates growth-strategy)
```

- **Specialists raise findings independently** -- no implicit consensus.
- **CEO arbitrates** when specialists disagree or when a finding has
  strategic implications (positioning, breaking change, naming, scope).
- **Growth Hacker is a side-channel** to the CEO: never blocks a
  specialist finding; annotates it with growth implications and
  escalates to the CEO when relevant.

## Conditional panelist: Auth Expert

Auth Expert is the only conditional panelist. Activate `auth-expert`
if either rule below matches.

1. **Fast-path file trigger.** Activate the Auth Expert lens
   immediately when the PR changes any of:
   - `src/apm_cli/core/auth.py`
   - `src/apm_cli/core/token_manager.py`
   - `src/apm_cli/core/azure_cli.py`
   - `src/apm_cli/deps/github_downloader.py`
   - `src/apm_cli/marketplace/client.py`
   - `src/apm_cli/utils/github_host.py`
   - `src/apm_cli/install/validation.py`
   - `src/apm_cli/deps/registry_proxy.py`

2. **Fallback self-check.** If no fast-path file matched, answer this
   before activating the lens:

   > Does this PR change authentication behavior, token management,
   > credential resolution, host classification used by `AuthResolver`,
   > git or HTTP authorization headers, or remote-host fallback
   > semantics? Answer YES or NO with one sentence citing the file(s).
   > If unsure, answer YES.

Routing rule:

- **YES** -> take the Auth Expert lens (per the Persona pass
  procedure) and capture its findings.
- **NO**  -> record `Auth Expert inactive reason: <one sentence
  citing the touched files>` in working notes; do not take the lens.
- Never use wildcard heuristics like `*auth*`, `*token*`, or
  `*credential*` as the sole trigger.

## Routing matrix

These routes choose which specialists are emphasised for a given PR
type. They do **not** change the verdict shape. The final comment
always uses every persona heading in `assets/verdict-template.md`;
the only persona that can render as `Not activated -- <reason>` is
Auth Expert (per the conditional rule above).

### Code review (architecture + logging)
1. Python Architect reviews structure / patterns / cross-file impact.
2. CLI Logging Expert reviews any output / logger changes.
3. CEO ratifies if the two disagree on abstraction vs consistency.

### CLI UX review
1. DevX UX Expert reviews command surface, flags, help, error wording.
2. CLI Logging Expert reviews how outputs are emitted (logger methods).
3. Growth Hacker annotates if the change affects first-run conversion.
4. CEO ratifies any naming / positioning calls.

### Security review
1. Supply Chain Security Expert maps the change to the threat model.
2. DevX UX Expert flags any ergonomics regression from the mitigation.
3. CEO arbitrates trade-offs; bias toward security on default behavior.

### Auth review (only when the conditional Auth Expert is activated)
1. Auth Expert maps the change against AuthResolver, token precedence,
   host classification, and credential helpers.
2. Supply Chain Security Expert checks for token-scoping or credential
   leakage implications.
3. CEO ratifies any default-behavior change.

### Release / comms review
1. CEO grounds the release framing in `gh` CLI stats.
2. Growth Hacker drafts hook + story angle; updates
   `WIP/growth-strategy.md` (gitignored maintainer-local; create if absent).
3. Specialists sanity-check any technical claims in release notes.

### Full panel review (non-trivial change)
1. Each mandatory specialist produces independent findings.
2. Auth Expert participates if the conditional rule above activates it.
3. Growth Hacker annotates findings with growth implications.
4. CEO synthesizes, resolves disagreements, makes the final call.
5. Surface decision and rationale to the author via the single verdict
   comment.

## Quality gates

A non-trivial change passes when:

- [ ] Python Architect: structure / patterns OK (or change explicitly
      justified)
- [ ] CLI Logging Expert: output paths route through CommandLogger,
      no direct `_rich_*` in commands
- [ ] DevX UX Expert: command surface familiar to npm/pip/cargo users,
      every error has a next action
- [ ] Supply Chain Security Expert: no new path / auth / integrity
      surface left unguarded; fails closed
- [ ] Auth Expert (only if activated): no regression to AuthResolver
      precedence, host classification, or credential leakage surface
- [ ] APM CEO: trade-offs ratified, breaking changes have CHANGELOG +
      migration line
- [ ] OSS Growth Hacker: conversion surfaces unaffected or improved;
      `WIP/growth-strategy.md` updated if relevant (maintainer-local;
      gitignored, never committed)

## Notes

- This skill orchestrates a panel **in your own context** -- you are
  the only agent. You load each persona's `.agent.md` reference file
  on demand (progressive disclosure), assume that persona's lens to
  produce its findings, then move to the next persona. Do NOT spawn
  sub-agents (no `task` tool dispatch) -- the panel is a sequence of
  reasoning passes inside one agent loop, not a multi-agent fan-out.
- Persona detail lives in the linked `.agent.md` files. Read each
  one when you switch to that persona; do not pre-load all of them.

## Execution checklist

When this skill is activated for a PR review, work through these
steps in order, in a single agent loop. Do not skip ahead and do not
emit any output before the final step.

1. Read the PR context (title, body, labels, changed files, diff).
   The orchestrating workflow already fetches this with `gh pr view`
   / `gh pr diff` -- do not re-fetch.
2. Resolve the **Auth Expert conditional case** using the rule in
   "Conditional panelist: Auth Expert" above. Record either an
   activation decision (and proceed to step 3) or an `Auth Expert
   inactive reason: <one sentence>` in working notes.
3. For each mandatory persona (plus `auth-expert` if activated),
   follow the **Persona pass procedure** below, one persona at a
   time. Do not try to play multiple personas in a single pass.
4. Run the **pre-arbitration completeness gate**:
   - Findings exist in working notes for the 5 mandatory specialists
     (Python Architect, CLI Logging Expert, DevX UX Expert, Supply
     Chain Security Expert, OSS Growth Hacker).
   - Exactly one of `Auth Expert findings` or `Auth Expert inactive
     reason` exists (neither = incomplete; both = inconsistent
     routing).
   - The Python Architect notes contain the OO / class mermaid
     diagram, the execution-flow mermaid diagram, and the Design
     patterns subsection declared in
     `../../agents/python-architect.agent.md`.
   - No persona section is missing or empty.
   If any check fails, redo that persona's pass and repeat the gate.
   Do not proceed to step 5 until the gate passes.
5. Take the **APM CEO** lens (load
   `../../agents/apm-ceo.agent.md`) and arbitrate over the collected
   findings -- still in your own context. CEO arbitration may run
   only after the completeness gate has passed.
6. Now (and only now) load `assets/verdict-template.md` and fill it
   in with the collected findings + arbitration.
7. Emit the filled template as exactly ONE comment via the workflow's
   `safe-outputs.add-comment` channel. Never call the GitHub API
   directly. This is the ONLY output emission for the entire panel
   run -- no per-persona comments, no progress comments.

### Persona pass procedure

For each persona, run this exact procedure in your own context:

1. Open the persona's `.agent.md` file (linked in the roster) and
   read its scope, lens, anti-patterns, and required return shape.
2. From that persona's lens, review the PR title/body/diff against
   the scope declared in the file.
3. Write the findings to working notes under
   `<persona-name>: <findings>` (or, for an inactive Auth Expert,
   `Auth Expert inactive reason: <one sentence>`).
4. Drop the persona lens before moving on. Do not emit any comment
   from inside a persona pass; persona findings stay in working
   notes until step 7 synthesizes them.

## Output contract

This contract is non-negotiable -- it is the difference between a
panel review that lands as one cohesive verdict and one that fragments
into per-persona noise.

- Produce **exactly one** comment per panel run. The
  `safe-outputs.add-comment.max` cap in the workflow is a fail-soft
  ceiling (currently 7, one per persona slot); the one-comment
  discipline lives here.
- Use `assets/verdict-template.md` as the comment body. Keep its
  section headings exactly as written. Adapt the body of each section
  to the PR. Do not invent new top-level sections or drop existing
  ones.
- CEO arbitration may run only after the completeness gate passes.
- Never emit findings as separate comments, intermediate progress
  comments, or "I will now invoke X" status comments.
- Load `assets/verdict-template.md` **at synthesis time only** (step
  6 above) -- not at activation, not while collecting findings.

## Gotchas

- **Roster invariant.** The frontmatter description, the roster
  table, the conditional-panelist rule, the verdict template, and the
  quality gates MUST agree on the persona set. If you change one,
  change all of them in the same edit. Description, roster, and
  template are the three places drift most often appears.
- **False-negative auth gotcha.** Auth regressions can be introduced
  from non-auth files that change the *inputs* to auth -- for
  example host classification, dependency parsing, clone URL
  construction, HTTP authorization headers, or call sites that
  bypass `AuthResolver`. If a diff changes how a remote host, org,
  token source, or fallback path is selected and you are not certain
  it is auth-neutral, activate `auth-expert`.
- **Bundle layout on the runner.** When this skill runs inside the
  PR-review agentic workflow, the APM bundle is unpacked under
  `.github/skills/apm-review-panel/` first, with `.apm/skills/...`
  as a fallback. The asset path is the same relative to the skill
  root (`assets/verdict-template.md`) in both layouts -- prefer the
  `.github/...` path when present.
- **No multi-persona-in-one-pass.** Each persona has its own
  `.agent.md` for a reason -- read it when you take that lens, write
  the findings, then drop the lens before moving on. Trying to be all
  personas in one reasoning pass is the most common cause of dropped
  findings and mixed voices.
- **Single-emission discipline is fragile under interruption.** If
  you find yourself wanting to "post a quick partial verdict and
  then update it", don't. Buffer in working notes; emit once.
