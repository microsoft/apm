# APM Principles

The product principles APM is held to. Maintainers use them to explain
scope and roadmap decisions. Automated triage and review may cite them
in recommendations; human authority is defined in
[GOVERNANCE.md](GOVERNANCE.md).

MANIFESTO = values. PRD = product pitch (currently framed at native
platform owners). **This file = the rejection contract.** When a
principle here conflicts with a feature request, the principle wins
by default and shifts only via an explicit, written trade-off in
the same PR.

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

Prioritize timely, respectful decisions on external contributions over
internal nice-to-haves. Give contributors clear scope and a review contact
before inviting implementation. Community priority does not require
accepting every proposal or promising review capacity the project lacks.
An honest deferral or decline is better than an indefinite promise.

## How this file is used

- Maintainers cite relevant principles when explaining scope decisions.
- `apm-ceo` cites by number in advisory prose.
- `batch-bug-shepherd` Phase 1.5 spawns one ceo subagent per
  triaged-LEGIT row, which returns a verdict + cited principle.
- `apm-triage-panel` CEO arbiter cites a principle on every
  `decline-with-reason` rubric outcome.
- `apm-review-panel` CEO synthesizer cites a principle when
  surfacing strategic implications in arbitration.

Changes to these principles require a public issue and a human decision
under [GOVERNANCE.md](GOVERNANCE.md#decisions-and-accountability).
Update affected documentation in the same PR. If a change alters a public
product contract, document the compatibility impact and migration in the
changelog. Agent recommendations do not ratify policy.
