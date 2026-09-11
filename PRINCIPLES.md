# APM Principles

The hard contract APM is held to. Every PR, every release, every
roadmap call cites these principles. The triage panel, the review
panel, and the batch-bug-shepherd Phase 1.5 strategic-alignment
gate cite them by number when accepting or rejecting work.

MANIFESTO = values. PRD = product pitch (currently framed at native
platform owners). **This file = the rejection contract.** When a
principle here conflicts with a feature request, the principle wins
by default and shifts only via an explicit, written trade-off in
the same PR.

P8 is a shipping precondition, not a trade-off: a paragraph in a PR
cannot waive required lifecycle evidence. Weakening it requires an
explicit strategic policy change.

## P1 -- No invented primitive frontmatter

APM emits to canonical schemas defined by upstream ecosystems:
agentskills.io / Anthropic Claude Skills, GitHub Copilot, Cursor,
Windsurf, Codex, Gemini, OpenCode. We do not invent `apm-*`
frontmatter keys, top-level fields, or hidden attributes that
downstream consumers must learn to honor APM-ness.

The primitive on disk must be readable, valid, and useful in the
consuming harness with zero APM-specific tooling.

Rejection example: "Add an `apm-priority` frontmatter key so
skills can self-rank in dispatch." NO. Dispatch ranking is the
harness's problem, not a schema mutation.

## P2 -- Multi-harness with traction gating

APM ships to every harness with demonstrable user traction. Today:
Copilot, Claude Code, Cursor, Windsurf, Codex, Gemini, OpenCode.
Adding a new harness requires evidence: published download / install
counts, named enterprise users, or a public ranking that places it
in the top tier of agent runtimes.

We do not chase the long tail. A harness with zero documented users
does not get a target adapter, period.

Rejection example: "Add a target for <obscure-harness>." NO unless
there is a citable traction number.

## P3 -- Vendor neutral by construction

No primitive APM produces or installs may bake in a preferred LLM
vendor, a preferred runtime, or a "works best with X" recommendation
in shipped output. README, docs, and CLI output must remain
runtime-agnostic where the surface is general.

Per-target adapters are allowed (they ARE the multi-harness promise);
preferential framing inside neutral surfaces is not.

Rejection example: "Default `apm run` to invoke Claude when no
runtime is configured." NO. Surface the missing config and require
an explicit choice.

## P4 -- UX is the floor, not a trade

APM's adoption funnel runs through `apm init`, `apm install`, and
`apm run`. No bug fix, hardening, security patch, or refactor lands
if it makes those commands harder, slower, more verbose, or more
confusing for a new user. The bug stays open until a UX-preserving
fix exists.

This is asymmetric on purpose: a bug bites the affected user once;
a bad install experience loses every future user silently.

Rejection example: "Fix #X by adding a required `--target` flag on
`apm install`." NO. Find a fix that preserves target inference, or
leave the issue open.

## P5 -- Portability over vendor lock-in

A primitive authored once must execute across every supported
harness without modification. Lock-in of any flavor -- vendor,
runtime, host -- is a regression.

## P6 -- Reliability over magic

Behavior must be predictable, auditable, and explainable in plain
English. No silent normalization, no opaque heuristics, no "the
agent decided." Every transformation has a name and a line in the
changelog.

## P7 -- Community over feature count

External-contributor PRs and issues triage before internal
nice-to-haves. A contributor lost is worse than a feature delayed.
Surface every external interaction at the top of the queue.

## P8 -- Lifecycle completeness

A feature is not complete when its entry command succeeds. Every
feature, fix, and lifecycle-affecting refactor must work across the
entire applicable command state machine before it ships. This binds
human developers and coding agents equally, including changes to
shared state that are not described as lifecycle work.

Account for every command and subcommand from the executable command
inventory: state transitions, observations, no-ops, refusals, and
failure/recovery. A command is not applicable only when it has no
relationship to the changed behavior or state, with a specific,
reviewable rationale. Missing test infrastructure is work to complete,
not an exemption.

Require both connected, deterministic real-CLI trajectories over
persistent isolated state and relevant dimensions exercised in the
generated state-machine model. Assert deployed content, ownership,
preservation of user data, and supported failure/recovery behavior.
Neither isolated command tests nor one of these two lifecycle layers
substitutes for the other. Refactors may reuse adequate existing
coverage; they need not manufacture new tests.

Missing, uncollected, skipped, xfailed, failed, stale, or merely asserted
required evidence blocks shipping. Evidence must apply to the actual
candidate. Unit-test totals, an unrelated green suite, and an advisory
ship recommendation cannot discharge this requirement. A missing
relevant model dimension cannot be deferred outside a feature's scope.

The operational authority is
[the lifecycle shipping rule](.apm/instructions/lifecycle.instructions.md).
Required CI enforces executable proof obligations; maintainers review
their applicability and assertion semantics. This is bounded evidence,
not a claim of exhaustive permutations or mathematical correctness.

Rejection example: "Global install works with this path format; update
and uninstall can be covered later." NO. Complete the lifecycle before
shipping the path format.

## How this file is used

- `apm-ceo` cites by number in arbitration prose.
- `batch-bug-shepherd` Phase 1.5 spawns one ceo subagent per
  triaged-LEGIT row, which returns a verdict + cited principle.
- `apm-triage-panel` CEO arbiter cites a principle on every
  `decline-with-reason` rubric outcome.
- `apm-review-panel` CEO synthesizer cites a principle when
  surfacing strategic implications in arbitration.

Any addition to this file requires the apm-ceo persona to ratify
and ships in a PR that updates MANIFESTO.md cross-refs in the same
commit. Removal of a principle is a breaking strategic change --
requires CHANGELOG entry, migration line, and explicit `BREAKING:`
prefix.
