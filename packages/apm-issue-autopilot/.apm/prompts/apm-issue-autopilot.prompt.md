---
name: APM Issue Autopilot
description: Drive any open microsoft/apm issue from intake to a mergeable PR via the apm-issue-autopilot skill -- triage-central, one consolidated review, escalate-by-default
interval: manual
mode: interactive
input:
  - targets: "Either a space-separated issue list (e.g. '#123 #456 #789') OR the literal word 'queue-all' to expand to every open issue eligible for autopilot intake"
---

# APM Issue Autopilot

Drive any open issue in microsoft/apm (bug, feature, docs, refactor,
perf) from raw intake to a mergeable PR, using the
**apm-issue-autopilot** skill as the working spec. Activate the skill
by name -- your harness loads it from wherever skills live for you
(this prompt is harness-agnostic and makes no assumption about on-disk
layout).

Targets for this run: **${input:targets}**

## Procedure

1. ACTIVATE the **apm-issue-autopilot** skill. Treat its contents as
   authoritative for the phase contract (intake -> per-issue triage
   -> ONE consolidated review gate -> implement accepted -> shepherd
   to mergeable -> final report) and the disciplines (triage is
   paramount, escalate-to-maintainer by default, exactly one human
   checkpoint, never auto-merge, mutation-break + lint gates,
   ASCII-only). If the skill is not available in this harness, abort
   with a clear error naming the skill.

2. SCOPE RESOLUTION:
   - If `${input:targets}` is `queue-all`: run
     `gh issue list --state open --json number,title,labels,body`
     and apply the skill's intake-eligibility filter.
   - Otherwise: parse the issue numbers from `${input:targets}` and
     fetch each via `gh issue view <n> --json
     number,title,body,labels`.

3. PRINT A BRIEF PLAN to the user BEFORE any fan-out: candidate
   count, wave shape, the disciplines that will be enforced, and
   where the ground-truth table will live (this session's plan.md).
   If `queue-all` produced more than 20 candidates, ASK for
   confirmation; otherwise proceed.

4. EXECUTE the skill phases in order. Honor the skill's single
   human-checkpoint contract: present ONE consolidated triage review
   for the whole batch (never drop-by-drop), and ESCALATE any
   doubtful issue to the maintainer rather than auto-implementing.

5. RENDER the final report from the template shipped with the skill
   at session end, with clickable GitHub issue / PR / author links.

## Delegation

All disciplines (triage-central escalation, ASCII-only, lint
contract, mutation-break gate, single-writer interlock per comment,
PR-in-flight cross-reference, schema-validation of subagent returns)
are owned by the **apm-issue-autopilot** skill and the skills it
composes (apm-triage-panel, shepherd-driver). This prompt does NOT
re-assert them -- the skill body is the single source of truth. If
the skill body evolves, this prompt inherits the change without edit.
