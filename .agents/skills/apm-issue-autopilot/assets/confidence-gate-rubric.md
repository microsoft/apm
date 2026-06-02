# Confidence gate rubric (Phase 2) - escalate by default

This is the load-bearing policy of apm-issue-autopilot. It maps a
triage decision to a `gate`. The bias is ESCALATE: auto-proceed is the
narrow exception, reached only for a clear, bounded, high-confidence
accept whose implementation brief is complete. When in doubt, escalate
to the maintainer. Triage is paramount; the gate is conservative.

The orchestrator applies this rubric per triaged row to compute the
`gate` column of the `proceed_manifest`. The maintainer then ratifies
or overrides each row in the single Phase 2 checkpoint.

## Gate values

- `auto-proceed` -- the orchestrator recommends implementing this
  issue. Still requires maintainer `approved` (or `overridden-to-
  proceed`) before any code is written.
- `escalate` -- doubtful; surface to the maintainer with the red
  flags. The maintainer MAY override to proceed (records
  `override_reason`).
- `terminal` -- triage resolved the issue without implementation
  (decline / duplicate / defer / auto-handle). Never auto-acted;
  surfaced for human action in Phase 7.

## Decision routing (first match wins)

1. decision is `decline-with-reason`, `duplicate-of`, `defer-later`,
   or `auto-handle` -> `terminal`. (auto-handle is a triage action
   the maintainer performs, NOT autopilot implementation.)
2. decision is `needs-design` -> `escalate`.
3. decision is `accept` AND any of the following -> `escalate`:
   - `red_flags` contains ANY of: breaking-change, auth-surface,
     security-surface, governance-surface, schema-migration,
     release-automation, multi-subsystem, unbounded-scope.
   - `type` is `type/architecture`, `type/automation`, or
     `type/release` (these escalate by default regardless of
     confidence -- they carry cross-cutting or supply-chain risk).
   - `confidence` is not `high`.
   - the `implementation_brief` is missing any of `deliverable`,
     `non_goals`, `acceptance_tests`, `docs_required`, `risk_surface`
     (the IMPLEMENTATION-READY gate -- separate from triage accept;
     a clean accept with an incomplete brief still escalates).
4. decision is `accept`, confidence `high`, no red flags, type in
   {type/bug, type/feature, type/docs, type/refactor,
   type/performance}, and a complete
   implementation brief -> `auto-proceed`.
5. ANY row not matched by rules 1-4 -> `escalate`. This is the
   escalate-by-default backstop: a malformed-but-schema-valid row, an
   unknown decision, or any ambiguity routes to the maintainer, never
   to silent auto-proceed.

## Why a separate implementation-ready gate

Triage `accept` answers "should this land?"; it does NOT answer "is
the work bounded and specified enough to implement unattended?". An
accept with a vague or partial brief is exactly where unattended
implementation drifts. Missing brief fields therefore route to the
human even when triage itself was confident.

## Maintainer decision (recorded in proceed_manifest)

After the digest, the maintainer marks each row:
- `approved` -- proceed as recommended (auto-proceed rows).
- `overridden` -- proceed despite an `escalate` gate; MUST carry an
  `override_reason`. Recorded as `overridden-to-proceed`. An override
  is VALID for implementation ONLY when the row carries a supported
  implementation type {type/bug, type/feature, type/docs,
  type/refactor, type/performance} AND
  a complete implementation brief. If the maintainer overrides a row
  whose type is unsupported (type/architecture, type/automation,
  type/release) or
  whose brief is incomplete, they MUST also reclassify the type and/or
  supply the missing brief fields in the same checkpoint reply;
  otherwise the override is rejected and the row stays escalated. The
  implementation pipeline NEVER receives an unsupported type (see
  task-implement-prompt.md type-routing fallback).
- `rejected` -- do not implement (an auto-proceed row the maintainer
  declines, or an escalated row left for human action).

Phases 3-6 select ONLY rows where the effective decision is
`approved` or `overridden-to-proceed`.
