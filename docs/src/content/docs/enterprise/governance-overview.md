---
title: Governance Overview
description: The enterprise ramp's map -- what APM lets you control, where each control runs, and which page in this chapter goes deep on each surface.
sidebar:
  order: 1
---

You own the trust boundary for AI agent context across N repos. This chapter is the spec for that scale. This page is the map.

APM's third promise -- *Org policy enforced at install time, before MCP touches disk* -- is delivered by four artifacts working together: a policy file your security team writes, an install-time gate that reads it, a CI check that re-runs it on every PR, and (optionally) a registry proxy that holds dependency traffic to a single network egress. Everything in this chapter drills into one of those four.

## The model in one paragraph

`apm-policy.yml` lives in `<org>/.github/` and is auto-discovered from each repo's git remote. Every `apm install` evaluates the resolved dependency graph -- including transitive MCP servers -- against the merged policy *before* writing anything to disk. `apm audit --ci` re-runs the same checks plus eight non-bypassable lockfile baselines and is wired into branch protection. Inheritance flows enterprise -> org -> repo and only tightens; a child policy cannot loosen a parent. Three enforcement levels: `block` (abort on violation), `warn` (continue with diagnostics), `off` (skip). Source: `src/apm_cli/policy/install_preflight.py`, `src/apm_cli/policy/inheritance.py`, `src/apm_cli/policy/ci_checks.py`.

The 30-second picture:

```
                       <org>/.github/apm-policy.yml
                                  |
                                  v
        +------------- discovery + merge -------------+
        |                                             |
        v                                             v
   apm install                                  apm audit --ci
   (install gate                                (8 baselines +
    + transitive                                 17 policy checks
    MCP preflight)                               in CI)
        |                                             |
        v                                             v
   files written or                          PR check passes
   exit 1 (no writes)                        or fails
```

## Surfaces of control

Four surfaces, four owners.

- **`apm-policy.yml`** -- the schema. Allow/deny lists for dependencies and MCP servers, MCP transport restrictions, compilation-target rules, manifest shape constraints, unmanaged-files action. Authored by the platform or security team in `<org>/.github/`. Schema: `src/apm_cli/policy/schema.py`.
- **Install-time enforcement** -- the gate. Three code paths share one outcome table: the install pipeline gate (`install/phases/policy_gate.py`, delegating to `policy/install_preflight.py`), the `--mcp <ref>` direct-install preflight, and the transitive-MCP second pass that runs after APM packages resolve their own MCP dependencies. Source: `src/apm_cli/policy/install_preflight.py`.
- **CI enforcement** -- `apm audit --ci`. Eight baseline lockfile checks always; the seventeen policy checks when a policy is discovered or supplied. The only enforcer of the audit-only fields (`compilation.strategy.enforce`, `manifest.required_fields`, `manifest.scripts`, `unmanaged_files.action`). Source: `src/apm_cli/policy/ci_checks.py`.
- **Built-in security gate** -- runs on every install regardless of policy. `SecurityGate` (`src/apm_cli/security/gate.py`) scans all primitive files for hidden Unicode and other content findings using `BLOCK_POLICY` before any file is written to a harness directory. Zero configuration. This sits underneath the policy engine: it cannot be turned off by `apm-policy.yml`.

A registry proxy (Artifactory or compatible) is the optional fifth surface for organizations that need all dependency traffic to flow through a single egress. See [Registry proxy](../registry-proxy/).

## Where each control runs

| Surface                        | When it runs                              | What it enforces                                                | Failure mode                              |
| ------------------------------ | ----------------------------------------- | --------------------------------------------------------------- | ----------------------------------------- |
| `SecurityGate` (built-in)      | Every install, before any file is written | Hidden Unicode and other critical content findings              | `[x]` Exit 1; nothing deployed            |
| Install pipeline gate          | Every install, after resolve, before targets | `dependencies.*`, `mcp.*` (direct), compilation-target after targets phase | `[x]` `enforcement: block` -> exit 1; `[!]` `warn` -> continue with summary |
| `--mcp <ref>` preflight        | `apm install --mcp owner/repo` only       | Same `mcp.*` rules as the gate, separate code path              | `[x]` Exit 1 before any MCP config is written |
| Transitive MCP preflight       | After APM packages resolve their MCPs     | `mcp.*` against transitive MCP servers                          | `[x]` Exit 1; APM packages stay, MCP configs not written |
| `apm audit --ci [--policy ...]`| In CI, on every PR                        | 8 lockfile baselines + 17 policy checks (incl. audit-only fields) | Exit 1; PR check fails                  |
| Registry proxy (optional)      | Every install download                    | All dependency traffic flows through the proxy; lockfile guard catches bypass | Bypass attempt -> `content_hash` mismatch on next install |

`apm compile`, `apm run`, and `apm pack` enforce zero policy. They trust what install placed on disk. APM is an install-time and CI gate, not a runtime sandbox. If you need runtime tool restriction, govern it through `mcp.*` and `dependencies.*` so the disallowed tooling never reaches the harness.

## Who does what

- **Platform / security team** -- writes `apm-policy.yml` in `<org>/.github/`. Owns the trust boundary via CODEOWNERS and branch protection on that repo. Decides the inheritance chain (enterprise -> org -> repo).
- **Consumers** -- run `apm install` inside the rails the policy defines. They discover what is in effect with `apm policy status` and what would be blocked with `apm install --dry-run`. See the consumer view in [Governance on the consumer ramp](../../consumer/governance-on-the-consumer-ramp/).
- **Producers** -- publish into marketplaces that the org's `apm-policy.yml` allows. A package that fails an org's `dependencies.allow` rule will not install in repos governed by that policy, regardless of where it was published. See [Publish to a marketplace](../../producer/publish-to-a-marketplace/).

Three quick commands cover most day-one questions:

```bash
apm policy status                    # what policy is in effect?
apm install --dry-run                # what would policy block on this manifest?
apm audit --ci --policy org          # full policy + baseline run, exit non-zero on violation
```

`apm policy status` reports the source of the active policy, the enforcement level, the cache age, the inheritance chain, and a count of effective rules per section. `apm install --dry-run` runs discovery and policy checks without writing to disk; each violation is reported as `Would be blocked by policy: <dep> -- <reason>`. `apm audit --ci` is the only command that exits non-zero on violation and is therefore the only one safe to wire into branch protection.

## Where the controls execute

Three execution surfaces, by environment:

- **Local CLI** -- `apm install` runs the install gate; `apm audit` runs locally for ad-hoc checks; `apm policy status` reports the active policy.
- **CI** -- `apm audit --ci` is the required check on the protected branch. This is the enforcement surface that survives `--no-policy` bypass attempts at the developer's machine.
- **Network egress** -- a registry proxy holds all package downloads to a single, observable path. The lockfile's `content_hash` plus `apm audit --ci` guarantees no in-band bypass. Optional but recommended at scale.

## Boundary statement

[i] APM enforces in the CLI and in CI. It is not a runtime sandbox. The model assumes branch protection on `<org>/.github/apm-policy.yml`, branch protection requiring `apm audit --ci` to pass, and either a registry proxy or the lockfile content-hash check covering download integrity. Take any one of those three away and the contract weakens.

## Read this chapter in order

1. [APM policy: getting started](../apm-policy-getting-started/) -- the smallest useful `apm-policy.yml` you can write today; what each top-level key does.
2. [Policy pilot](../policy-pilot/) -- the warn-first rollout pattern: ship `enforcement: warn`, measure violations, flip to `block`.
3. [Enforce in CI](../enforce-in-ci/) -- wiring `apm audit --ci` as a required check; `--policy <scope>` and the audit-only fields it exposes.
4. [Drift detection](../drift-detection/) -- the eight non-bypassable lockfile baselines and what each one catches.
5. [Registry proxy](../registry-proxy/) -- routing dependency traffic through Artifactory or a compatible proxy; air-gapped CI playbook.
6. [Security and supply chain](../security-and-supply-chain/) -- the threat model `SecurityGate` defends against; MCP trust boundary; provenance.
7. [Adoption playbook](../adoption-playbook/) -- staged rollout across N repos; pilot team -> org-wide -> required check.
8. [GitHub rulesets](../github-rulesets/) -- the GitHub-side configuration that makes `apm audit --ci` authoritative.

[>] Start with [APM policy: getting started](../apm-policy-getting-started/) and write your first policy in 10 minutes. Come back here when you need the map.
