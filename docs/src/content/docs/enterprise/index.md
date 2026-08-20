---
title: "Enterprise"
description: "APM for organizations: making the case, rolling out at scale, securing the agent supply chain, and governing dependencies by policy."
sidebar:
  order: 1
---

You have seen how to consume and produce primitives. This section is for teams that need to control which primitives are allowed, how they are secured, and how compliance is maintained at scale. It delivers APM's third promise: **every AI package your developers install is governed by org policy before it touches disk.**

Read it as five phases. Most teams move through them in order; jump to the phase you are in.

## Decide

Build the case and plan the rollout.

- [Making the case](./making-the-case/) -- problem-at-scale narrative, talking points by audience, objection handling, sample RFC, ROI framework.
- [Adoption playbook](./adoption-playbook/) -- phased rollout from pilot team to organization-wide, with milestones, success metrics, and rollback options.

## Secure

Understand the install-time security model and the execution-trust surfaces.

- [Security model](./security/) -- pre-deploy gate, content scanners, hidden-Unicode threat model, integrity, provenance, and the MCP trust boundary. Read verbatim by procurement and security reviewers.
- [Lifecycle scripts](./lifecycle-scripts/) -- custom actions at install/update/uninstall, and the trust model that decides what runs and who authorizes it.

## Author policy

Write `apm-policy.yml` and test it before it bites.

- [Policy files](./apm-policy/) -- conceptual model of `apm-policy.yml` plus your first policy in 20 minutes.
- [Policy pilot](./policy-pilot/) -- the warn-then-block rollout so a new rule does not break every repo on day one.
- [Policy reference](./policy-reference/) -- complete schema for every field.

## Enforce

Make the policy authoritative on every pull request.

- [Enforce in CI](./enforce-in-ci/) -- wire `apm audit --ci` as a required check.
- [Drift detection](./drift-detection/) -- the eight non-bypassable lockfile baselines and what each catches.
- [GitHub rulesets](./github-rulesets/) -- the GitHub-side config that makes the check unbypassable.

## Operate

Run governance at organization scale.

- [Registry proxy](./registry-proxy/) -- route all dependency traffic through Artifactory or a compatible proxy; air-gapped CI playbook.
- [Governance deep-dive](./governance-guide/) -- the full trust contract: bypass surfaces, install-gate guarantees, audit-log schema, known gaps. The due-diligence reference for a CISO deciding to make `apm audit --ci` a required check.
