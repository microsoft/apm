---
title: "Enterprise"
description: "APM for organizations: making the case, rolling out at scale, securing the agent supply chain, and governing dependencies by policy."
sidebar:
  order: 1
---

APM for organizations rests on three pillars:

- **[Portable by manifest](../quickstart/)** -- one `apm.yml` declares every dependency; `apm.lock.yaml` pins exact versions; every developer and every CI run gets the same agent setup.
- **[Secure by default](./security/)** -- `apm install` scans every package for hidden Unicode and other tampering before agents read it. Attack surface, scanners, and the MCP trust boundary are documented for procurement review.
- **[Governed by policy](./governance-guide/)** -- `apm-policy.yml` lets platform teams allow-list dependencies, restrict deploy targets, and enforce trust rules at install time across every repo, from a single source of truth.

## Where to start

| If you are... | Start here |
|---|---|
| A CISO or security reviewer | [Security Model](./security/) -> [Governance](./governance-guide/) -> [Registry Proxy & Air-gapped](./registry-proxy/) |
| A VP of Engineering or Tech Lead evaluating APM | [Governance](./governance-guide/) -> [Adoption Playbook](./adoption-playbook/) |
| A platform engineer rolling out APM org-wide | [Adoption Playbook](./adoption-playbook/) -> [Registry Proxy & Air-gapped](./registry-proxy/) |
| A champion building an internal pitch | [Making the Case](./making-the-case/) -> [Adoption Playbook](./adoption-playbook/) |
| An engineer authoring policy | [Policy Files](./apm-policy/) -> [Policy Reference](./policy-reference/) |

## Section map

- [Making the Case](./making-the-case/) -- problem-at-scale narrative, talking points by audience, objection handling, sample RFC, ROI framework.
- [Adoption Playbook](./adoption-playbook/) -- phased rollout from pilot team to organization-wide, with milestones, success metrics, and rollback options.
- [Security Model](./security/) -- supply-chain posture: pre-deploy gate, content scanners, hidden-Unicode threat model, MCP trust boundary. Consumed verbatim by procurement and security reviewers.
- [Governance](./governance-guide/) -- the flagship trust contract: bypass surfaces, install-gate guarantees, audit-log schema, rollout playbook, known gaps. Read this if you are deciding whether to make `apm audit --ci` a required check.
- [Registry Proxy & Air-gapped](./registry-proxy/) -- route dependency and marketplace traffic through Artifactory or a compatible proxy; bypass-prevention contract; air-gapped CI playbook for both online-proxy and offline-bundle shapes.
- [Policy Files](./apm-policy/) -- conceptual model of `apm-policy.yml`: what it is, what it declares, how to start one.
- [Policy Reference](./policy-reference/) -- complete schema for every `apm-policy.yml` field.
