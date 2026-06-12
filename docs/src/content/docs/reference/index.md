---
title: Reference
description: CLI commands, schemas, and specifications for APM.
sidebar:
  order: 0
---

This section is the authoritative reference for APM. If you're learning APM, start in the [consumer ramp](../consumer/) or [producer ramp](../producer/) instead -- the reference assumes you know what you're looking for.

## CLI commands

Per-command pages live under [`reference/cli/`](./cli/install/). Grouped by lifecycle phase:

| Phase                  | Commands                                                                         |
|------------------------|----------------------------------------------------------------------------------|
| Project setup          | [`init`](./cli/init/), [`install`](./cli/install/), [`update`](./cli/update/), [`uninstall`](./cli/uninstall/) |
| Inspect and audit      | [`view`](./cli/view/), [`deps`](./cli/deps/), [`outdated`](./cli/outdated/), [`list`](./cli/list/), [`audit`](./cli/audit/) |
| Compile and integrate  | [`compile`](./cli/compile/), [`prune`](./cli/prune/), [`targets`](./cli/targets/), [`runtime`](./cli/runtime/) |
| Cache and config       | [`cache`](./cli/cache/), [`config`](./cli/config/)                               |
| Run scripts            | [`run`](./cli/run/)                                                              |
| Author and distribute  | [`pack`](./cli/pack/), [`unpack`](./cli/unpack/), [`preview`](./cli/preview/), [`marketplace`](./cli/marketplace/), [`search`](./cli/search/) |
| Governance             | [`policy`](./cli/policy/), [`mcp`](./cli/mcp/)                                   |
| Experimental           | [`experimental`](./cli/experimental/)                                            |

## Schemas and specifications

| Doc                                                          | What it specifies                                                |
|--------------------------------------------------------------|------------------------------------------------------------------|
| [Manifest schema](./manifest-schema/)                        | Every field in `apm.yml`                                         |
| [Lockfile spec](./lockfile-spec/)                            | `apm.lock.yaml` schema, semantics, and integrity contract        |
| [Policy schema](./policy-schema/)                            | `apm-policy.yml` rules, merge semantics, severity                |
| [Targets matrix](./targets-matrix/)                          | Which primitive types each target (Copilot, Claude, ...) supports |
| [Baseline checks](./baseline-checks/)                        | The CI checks `apm audit --ci` runs                              |
| [Environment variables](./environment-variables/)            | Every env var APM reads, with precedence                         |
| [Primitive types](./primitive-types/)                        | Skill, prompt, instruction, agent, hook, command                 |
| [Package types](./package-types/)                            | APM packages, plugins, marketplaces                              |
| [Examples](./examples/)                                      | Worked end-to-end examples                                       |
| [Experimental](./experimental/)                              | Surface area still subject to change                             |

## Conventions used in reference pages

- `<placeholder>` -- you must substitute a real value.
- `[optional]` -- argument or flag is optional.
- All commands assume `apm` is on `PATH`; install instructions are in the [Quickstart](../quickstart/).
- Status symbols in CLI output: `[+]` success, `[!]` warning, `[x]` error, `[i]` info, `[*]` action, `[>]` running.
