---
title: "Adoption Playbook"
description: "A sequenced rollout plan for a platform team adopting APM across an organization, with phase gates, owners, and pitfalls."
sidebar:
  order: 8
---

This is the order to roll APM out. Five phases, each with a single owner,
a deliverable, and a gate to clear before moving on. Read the
[Governance overview](../governance-overview/) page first if you have not -- it is the
contract this playbook operationalizes.

| Phase | Duration | Owner | Gate to advance |
|---|---|---|---|
| 1. Discover | 1-2 weeks | Platform team | Shadow install report reviewed |
| 2. Pilot | ~1 month | One product team + platform | Pilot CI green for 2 weeks in `warn` mode |
| 3. Harden | ~1 month | Security + Platform | Policy in `block`; proxy live if required |
| 4. Scale | Ongoing | Platform + DevRel | 3+ teams onboarded; KPIs reported |
| 5. Sustain | Steady state | Platform | Monthly drift sweep + lockfile review |

Do not skip phases. Each one buys evidence the next one needs.

---

## Phase 1 -- Discover (1-2 weeks)

A small group runs `apm install` against existing repos in shadow mode to
see what would land and what would break. No commits, no policy, no CI
yet.

**Owner:** Platform team (2-3 engineers).

**Deliverables:**

- A list of 5-10 representative repos covered.
- A spreadsheet of what `apm install` would deploy in each repo and which
  files it would conflict with.
- A first-pass inventory of MCP servers in scope.
- Rough sizing: how many repos, how many primitives, how many distinct
  agent harnesses across the org. Use the
  [primitives and targets](../../concepts/primitives-and-targets/) model
  as the vocabulary.

**How to run it:**

```bash
apm install --dry-run
apm audit
```

`--dry-run` reports what would change without writing. `apm audit` runs
the [drift detection](../drift-detection/) and content scans
that are on by default.

**Gate to advance:** the platform team can answer, in one slide: "what
breaks if we turn this on tomorrow, and for whom?"

**Common pitfalls:** picking only greenfield repos (include at least one
with hand-edited `.github/`, `.cursor/`, or `.claude/` content -- that is
where drift findings appear); treating the shadow run as a buy decision
when it is a sizing exercise.

---

## Phase 2 -- Pilot (~1 month)

One production team adopts APM end to end: manifest, lockfile, CI audit,
and policy in `warn` mode. The platform team rides along.

**Owners:** Pilot team tech lead (workflow), platform team (policy + CI).

**Deliverables:**

- `apm.yml` and `apm.lock.yaml` committed in the pilot repo.
- An org policy file at `<org>/.github/apm-policy.yml` with
  `enforcement: warn`. See [Get started with apm-policy.yml](../apm-policy-getting-started/) for the mental
  model and [Policy Reference](../policy-reference/) for fields.
- `apm audit --ci` wired into the pilot repo's required checks. See the
  [CI Policy Enforcement](../enforce-in-ci/) guide.
- A weekly review of warnings the policy would have blocked.

**Why `warn` first:** it lets you tune the allow-lists against real
traffic without ever red-marking a PR. The
[Governance overview](../governance-overview/) page documents the bypass surface so
you know exactly what `warn` mode does and does not promise.

**Gate to advance:** two consecutive weeks where every pilot PR passes
`apm audit --ci` cleanly, and every policy warning has been triaged
(allow-listed, fixed, or accepted).

**Common pitfalls:** enforcing on day one (you will block legitimate
work and lose the team -- stay in `warn` until the warning rate is near
zero); skipping the lockfile commit (reproducibility is the whole point
of the pilot); letting the pilot team author org policy (policy belongs
in the `.github` repo behind branch protection; the pilot only
consumes it).

---

## Phase 3 -- Harden (~1 month)

Tighten policy from `warn` to `block`, add a registry proxy if your org
requires one, and stand up internal marketplaces so the next teams have
something curated to install from.

**Owners:** Security (policy contents, proxy contract), Platform
(rollout, marketplace), Pilot team (regression watch).

**Deliverables:**

- `enforcement: block` set on the org policy. The pilot repo is the
  canary -- if its CI stays green for a week, the gate is real.
- If your org standardizes on Artifactory or an equivalent: registry
  proxy live, with the bypass-prevention contract in
  [Registry Proxy & Air-gapped](../registry-proxy/) verified.
- One or more org marketplaces published, replacing ad-hoc package
  references. See [Publish to a marketplace](../../producer/publish-to-a-marketplace/) for
  the authoring side.
- A short internal page documenting which packages are blessed and how
  to request a new one.

**Gate to advance:** a fresh repo, owned by neither platform nor the
pilot team, can run `apm install` against the org policy and the proxy
end-to-end with no manual intervention.

**Common pitfalls:** flipping `block` org-wide before the pilot has run
a week on the new setting (always canary); building the marketplace
before you know what teams want (the Discover inventory is the input);
treating the proxy as optional when your security org mandates
Artifactory for npm and PyPI (APM is the same conversation -- do not
ship on direct GitHub fetches).

---

## Phase 4 -- Scale

Roll out to more teams. Move from "the platform team helps you adopt"
to "self-service onboarding."

**Owners:** Platform team (enablement), DevRel or internal champions
(per-team pull), team tech leads (per-repo work).

**Deliverables:**

- An onboarding doc that points new teams at the
  [consumer ramp](../../consumer/install-packages/) for daily flow and
  this playbook's Phase 2 checklist for setup.
- Adoption KPIs reported monthly:
  - Repos with `apm.yml` committed.
  - `apm audit --ci` pass rate per week.
  - Number of distinct packages installed from org marketplaces.
  - Drift findings closed vs opened (trend, not absolute).
- A backlog of policy refinements driven by incoming team feedback.

**Gate to "done with rollout":** the platform team is no longer in the
critical path for a new team to adopt APM. New teams onboard without
filing a ticket.

**Common pitfalls:** mandating adoption without offering a marketplace
worth installing from (carrot before stick); measuring `apm.yml` count
and nothing else (audit pass rate and drift trend are the leading
indicators -- manifest count is vanity); letting policy ossify
(schedule a quarterly review).

---

## Phase 5 -- Sustain

Steady-state operations.

**Owner:** Platform team (rotating on-call).

**Cadence:**

- **Weekly:** triage `apm audit --ci` failures across the org. Most are
  drift; the [Drift Detection](../drift-detection/) guide is
  the runbook.
- **Monthly:** lockfile review on long-lived repos. Bump pinned
  versions of org-required packages; close drift findings older than
  one cycle.
- **Quarterly:** marketplace refresh. Retire unused packages. Promote
  internal tools that have proven themselves into the org marketplace.
  Re-read the [Governance overview](../governance-overview/) known-gaps section
  against the current APM version.

**Health signals to watch:**

- Audit pass rate trending down -> drift is accumulating; investigate
  before it becomes a release blocker.
- Policy warning rate climbing -> teams are reaching for packages the
  marketplace does not cover; consider adding them.
- Time-to-onboard a new team creeping up -> the onboarding doc has
  drifted from reality; refresh it.

**Common pitfalls:** no named owner ("the platform team" is not an
owner; a named on-call is); treating policy as set-and-forget (the
threat model and the agent ecosystem move; the policy must too).

---

## Related

- [Making the Case](../making-the-case/) -- the pitch deck inputs for
  Phase 1 stakeholder buy-in.
- [Governance overview](../governance-overview/) -- the trust contract this playbook
  operationalizes.
- [Security and supply chain](../security-and-supply-chain/) -- the procurement-grade answer for
  Phase 1 review.
- [Get started with apm-policy.yml](../apm-policy-getting-started/) and [Policy Reference](../policy-reference/)
  -- what to put in `apm-policy.yml` for Phase 2 and Phase 3.
- [Registry Proxy & Air-gapped](../registry-proxy/) -- Phase 3 proxy
  rollout.
