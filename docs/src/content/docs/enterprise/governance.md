---
title: "Governance & Compliance"
description: "Enforce AI agent configuration policies with lock files, audit trails, and CI gates."
sidebar:
  order: 3
---

For org-policy enforcement (apm-policy.yml, apm audit --ci, install gate), see the [Governance Guide](../governance-guide/). This page focuses on the lockfile audit trail and forensic recipes.

:::caution[Experimental — Policy Engine]
Sections on this page covering organization policy enforcement (`apm audit --ci --policy`, `apm-policy.yml`, inheritance chains) describe an early preview feature for testing and feedback. Lock-file based governance (`apm audit --ci` baseline checks) is stable. The policy engine layer on top may change based on community input.
:::

## The governance challenge

**Twelve teams. Four agent stacks. One security review.** As AI agents become integral to software development, organizations face questions that traditional tooling was never designed to answer:

- **Incident response.** What agent instructions were active during a production incident?
- **Change management.** Who approved this agent configuration change, and when?
- **Policy enforcement.** Are all teams using approved plugins and instruction sources?
- **Audit readiness.** Can we produce evidence of agent configuration state at any point in time?

APM answers all four with two files: `apm.lock.yaml` records what was deployed; [`apm-policy.yml`](../apm-policy/) defines what is allowed. Both are git-tracked, both are reviewable, both reconstruct any historical state with one git command.

---

## APM's governance pipeline

Agent governance in APM follows a four-stage pipeline:

```
apm.yml (declare) -> apm.lock.yaml (pin) -> apm audit (verify) -> CI gate (enforce)
```

| Stage | Purpose | Artifact |
|-------|---------|----------|
| **Declare** | Define dependencies and their sources | `apm.yml` |
| **Pin** | Resolve every dependency to an exact commit | `apm.lock.yaml` |
| **Verify** | Scan deployed content for hidden threats | `apm audit` output |
| **Enforce** | Block merges when verification fails | Required status check |

Each stage builds on the previous one. The lock file provides the audit trail, content scanning verifies file safety, and the CI gate prevents unapproved changes from reaching protected branches.

---

## Lock file as audit trail

The `apm.lock.yaml` file is the single source of truth for what agent configuration is deployed. Every dependency is pinned to an exact commit SHA, making the lock file a complete, point-in-time record of agent state.

### What the lock file captures

```yaml
lockfile_version: '1'
generated_at: '2025-03-11T18:13:45.123456+00:00'
apm_version: 0.25.0
dependencies:
  - repo_url: https://github.com/contoso/agent-standards.git
    resolved_commit: a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0
    resolved_ref: main
    version: 2.1.0
    depth: 1
    deployed_files:
      - .github/agents/code-review.md
      - .github/agents/security-scan.md
  - repo_url: https://github.com/contoso/shared-skills.git
    virtual_path: shared-skills/api-design
    resolved_commit: f8e7d6c5b4a3f2e1d0c9b8a7f6e5d4c3b2a1f0e9
    resolved_ref: v1.4.0
    is_virtual: true
    depth: 2
    resolved_by: contoso/agent-standards.git
    deployed_files:
      - .github/skills/api-design/
```

Key fields for governance:

- **`resolved_commit`**: Exact commit SHA. No ambiguity about what code was deployed.
- **`depth`**: `1` for direct dependencies, `2+` for transitive. Identifies supply chain depth.
- **`resolved_by`**: For transitive dependencies, traces which direct dependency introduced them.
- **`deployed_files`**: Explicit list of files placed in the repository.
- **`generated_at`** and **`apm_version`**: Metadata for forensic reconstruction.

### Using git history for auditing

Because `apm.lock.yaml` is a committed file, standard git operations answer governance questions directly:

```bash
# Full history of every agent configuration change
git log --oneline apm.lock.yaml

# Who changed agent config, and when
git log --format="%h %ai %an: %s" apm.lock.yaml

# What was the exact agent configuration at release v4.2.1
git show v4.2.1:apm.lock.yaml

# Diff agent config between two releases
git diff v4.1.0..v4.2.1 -- apm.lock.yaml

# Find the commit that introduced a specific dependency
git log -p --all -S 'contoso/agent-standards' -- apm.lock.yaml
```

No additional tooling is required. The lock file turns git into an agent configuration audit log.

---

## Content scanning

APM scans deployed files for hidden Unicode threats. `apm install` blocks critical findings automatically; `apm audit` provides reporting and remediation. See [Content scanning](../security/#content-scanning) in the security model for the threat model, severity levels, and usage details.

---

## CI enforcement

`apm install` is the CI gate — it blocks deployment of packages with critical content findings, exiting with code 1. No additional configuration is needed.

`apm audit --ci` adds lockfile consistency checking (6 baseline checks, no configuration). Add `--policy org` to enforce organizational rules (16 additional policy checks).

### Two-tier enforcement

| Tier | Command | Checks | Requires policy |
|------|---------|--------|-----------------|
| Baseline | `apm audit --ci` | 6 lockfile consistency checks | No |
| Policy | `apm audit --ci --policy org` | 6 baseline + 16 policy checks | Yes |

Baseline catches configuration drift. Policy enforces organizational standards.

For step-by-step setup including SARIF integration and GitHub Code Scanning, see the [CI Policy Enforcement guide](../../guides/ci-policy-setup/).

---

## Organization policy governance

For organization-wide policy enforcement (`apm-policy.yml`, install gate, `apm audit --ci --policy org`, inheritance chains, the bypass contract, and the rollout playbook), see the [Governance Guide](../governance-guide/). The mental model for the policy file itself lives in [`apm-policy.yml`](../apm-policy/); the schema is in the [Policy Reference](../policy-reference/).

---

## Drift detection

:::note[Planned Feature]
`apm audit --drift` is not yet available. Currently, use `apm audit --ci --policy` with the `unmanaged_files` policy section to detect files in governance directories not tracked by APM. See the [Policy Reference](../policy-reference/#unmanaged_files) for configuration.
:::

---

## Constitution injection

A constitution is an organization-wide rules block applied to all compiled agent instructions. Define it in `memory/constitution.md` and APM injects it into every compilation output with hash verification:

```markdown
<!-- memory/constitution.md -->
## Organization Standards

- All code suggestions must include error handling.
- Never suggest credentials or secrets in code.
- Follow the organization's API design guidelines.
- Escalate security-sensitive operations to a human reviewer.
```

The constitution block is rendered into compiled output with a SHA-256 hash, enabling drift detection if the block is tampered with after compilation:

```markdown
<!-- APM_CONSTITUTION_BEGIN -->
hash: e3b0c44298fc1c14 path: memory/constitution.md
[Constitution content]
<!-- APM_CONSTITUTION_END -->
```

This ensures that organizational rules are consistently applied across all teams and cannot be silently bypassed.

---

## Integration with GitHub Rulesets

GitHub Rulesets enforce APM governance at scale by configuring the APM workflow as a required status check across multiple repositories. For setup instructions, see the [GitHub Rulesets integration guide](../../integrations/github-rulesets/).

---

## Compliance scenarios

### SOC 2 evidence

SOC 2 audits require evidence that configuration changes are authorized and traceable. APM's lock file provides this:

- **Change authorization.** Every `apm.lock.yaml` change goes through a PR, requiring review and approval.
- **Change history.** `git log apm.lock.yaml` produces a complete, tamper-evident history of every agent configuration change with author, timestamp, and diff.
- **Point-in-time state.** `git show <tag>:apm.lock.yaml` reconstructs the exact agent configuration active at any release.

Link auditors directly to the lock file history in your repository. No separate audit system is needed.

### Security audit

When a security review requires understanding what instructions agents were following:

```bash
# What agent configuration was active at the time of the incident
git show <commit-at-incident-time>:apm.lock.yaml

# What files were deployed by a specific package
grep -A 10 'contoso/agent-standards' apm.lock.yaml

# Full diff of agent config changes in the last 90 days
git log --since="90 days ago" -p -- apm.lock.yaml
```

The lock file answers "what was running" without requiring access to the original package repositories. The `resolved_commit` field points to the exact source code that was deployed.

### Change management

APM enforces change management by design:

1. **Declaration.** Changes start in `apm.yml`, which is a committed, reviewable file.
2. **Resolution.** `apm install` resolves declarations to exact commits in `apm.lock.yaml`.
3. **Review.** Both files are included in the PR diff for peer review.
4. **Verification.** `apm audit --ci` verifies lockfile consistency. Add `--policy org` for organizational policy enforcement.
5. **Traceability.** Git history provides a permanent record of who changed what and when.

No agent configuration change can reach a protected branch without passing through this pipeline.

---

## Summary

| Capability | Mechanism | Status |
|---|---|---|
| Dependency pinning | `apm.lock.yaml` with exact commit SHAs | Available |
| Audit trail | Git history of `apm.lock.yaml` | Available |
| Constitution injection | `memory/constitution.md` with hash verification | Available |
| Transitive MCP trust control | `--trust-transitive-mcp` flag | Available |
| Content scanning | Pre-deploy gate blocks critical hidden Unicode; `apm audit` for on-demand checks | Available |
| CI enforcement (content scanning) | Built into `apm install`; `apm audit` for SARIF reporting | Available |
| CI enforcement (lockfile consistency) | `apm audit --ci` for manifest/lockfile verification | Available |
| Organization policy enforcement | `apm audit --ci --policy org` with `apm-policy.yml` | Available |
| Policy inheritance | `extends:` for enterprise → org → repo chains | Available |
| Drift detection | `apm audit --drift` | Planned |
| GitHub Rulesets integration | Required status checks | Available |

For CI/CD setup details, see the [CI/CD integration guide](../../integrations/ci-cd/). For policy schema and check details, see the [Policy Reference](../policy-reference/). For lock file internals, see [Key Concepts](../../introduction/key-concepts/).
