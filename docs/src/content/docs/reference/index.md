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
| Project setup          | [`init`](./cli/init/), [`auth`](./cli/auth/), [`install`](./cli/install/), [`lock`](./cli/lock/), [`update`](./cli/update/), [`uninstall`](./cli/uninstall/) |
| Inspect and audit      | [`view`](./cli/view/), [`deps`](./cli/deps/), [`outdated`](./cli/outdated/), [`list`](./cli/list/), [`find`](./cli/find/), [`audit`](./cli/audit/), [`doctor`](./cli/doctor/) |
| Compile and integrate  | [`compile`](./cli/compile/), [`prune`](./cli/prune/), [`targets`](./cli/targets/), [`runtime`](./cli/runtime/) |
| Cache and config       | [`cache`](./cli/cache/), [`config`](./cli/config/)                               |
| Run scripts            | [`run`](./cli/run/)                                                              |
| Author and distribute  | [`pack`](./cli/pack/), [`unpack`](./cli/unpack/), [`preview`](./cli/preview/), [`plugin`](./cli/plugin/), [`publish`](./cli/publish/), [`export-patch`](./cli/export-patch/), [`lifecycle`](./cli/lifecycle/), [`marketplace`](./cli/marketplace/), [`search`](./cli/search/), [`self-update`](./cli/self-update/) |
| Governance             | [`approve`](./cli/approve/), [`deny`](./cli/deny/), [`policy`](./cli/policy/), [`mcp`](./cli/mcp/) |
| Experimental           | [`experimental`](./cli/experimental/)                                            |

## Deprecations

Deprecated surfaces still work. Each row names the replacement, the release that deprecated it, and the removal version its warning announces. "Not scheduled" means no removal version is set. A removal version older than your APM still works: that horizon passed without a removal.

<!-- BEGIN GENERATED: deprecations (scripts/render_deprecations_table.py) -->
| Deprecated | Use instead | Deprecated since | Removal | How APM tells you |
|---|---|---|---|---|
| `apm config` key `auto-integrate` | None (it has no effect); remove it | v0.37.0 | not scheduled | Warning |
| `vscode` target alias | `copilot` | v0.37.0 | not scheduled | Warning |
| Singular `target:` in `apm.yml` | `targets:` (the warning prints the exact value) | v0.37.0 | not scheduled | Warning |
| `compilation.target` in `apm.yml` | None (ignored); delete it | v0.37.0 | not scheduled | Warning |
| `all` in `apm.yml` `targets:` | Omit the field | unknown | not scheduled | Warning |
| `apm compile --target all` | `apm compile --all` | unknown | not scheduled | Warning |
| `allowExecutables:` in `apm.yml` | `executables.allow` (migrated by `apm approve`/`apm deny`) | v0.22.0 | not scheduled | Warning |
| `bin_deploy:` in `apm-policy.yml` | `executables` (`deny_all`/`deny`) | v0.22.0 | not scheduled | Warning |
| `apm pack --target` | None (bundles are target-agnostic) | unknown | not scheduled | Warning |
| `apm unpack` | `apm install <bundle-path>` | unknown | not scheduled | Warning |
| `apm deps update` | `apm update` | v0.17.0 | not scheduled | Warning |
| `apm update` outside an `apm.yml` project | `apm self-update` | v0.13.0 | not scheduled | Warning |
| `apm update --check` | `apm update --dry-run` or `apm self-update --check` | unknown | not scheduled | Warning |
| Standalone `marketplace.yml` | `marketplace:` block in `apm.yml` (`apm marketplace migrate`) | v0.11.0 | not scheduled | Warning |
| List or string form of `marketplace.outputs` | Map form (`outputs: {claude: {}}`) | v0.14.0 | v0.15 | Collected, not printed |
| `--target agents` | `--target copilot` or `--target agent-skills` | v0.12.0 | v1.0 | Warning |
| `<VAR>` placeholders in MCP server config | `${VAR}` | unknown | v1.0 | Warning |
| Python API `SimpleRegistryClient.get_server_info()` | `SimpleRegistryClient.get_server()` | v0.14.0 | not scheduled | Python `DeprecationWarning` (hidden by default) |
| `ARTIFACTORY_BASE_URL` | `PROXY_REGISTRY_URL` | v0.8.12 | not scheduled | Python `DeprecationWarning` (hidden by default) |
| `ARTIFACTORY_APM_TOKEN` | `PROXY_REGISTRY_TOKEN` | unknown | not scheduled | Python `DeprecationWarning` (hidden by default) |
| `ARTIFACTORY_ONLY` | `PROXY_REGISTRY_ONLY` | unknown | not scheduled | Python `DeprecationWarning` (hidden by default) |

Deprecated without a warning today:

| Deprecated | Use instead | Kind |
|---|---|---|
| `apm install --update` | `apm update` (or `apm update --yes` in CI) | flag |
| `apm marketplace add --branch` | `--ref` | flag |
| User `approvals.yml` store | `executables` in the user `config.json` (migrated on first read) | file |
| `PolicyBlockError` | `PolicyViolationError` | python-api |
| `get_lockfile_installed_paths()` | `LockFile.installed_paths_for_project()` | python-api |
| `HookIntegrator.integrate_package_hooks_{claude,cursor,codex}()` | `HookIntegrator.integrate_hooks_for_target()` | python-api |
| `CommandIntegrator.sync_integration()`, `.sync_integration_opencode()` | `sync_for_target()` | python-api |
| `AgentIntegrator.sync_integration_{cursor,opencode}()` | `sync_for_target()` | python-api |
| `InstructionIntegrator.sync_integration_{cursor,claude}()` | `sync_for_target()` | python-api |
| `InstallLogger.download_complete(ref_suffix=)` | `ref=` and `sha=` | python-api |
| `render_marketplace_yml_template()` | `render_marketplace_block()` | python-api |
<!-- END GENERATED: deprecations -->

## Schemas and specifications

| Doc                                                          | What it specifies                                                |
|--------------------------------------------------------------|------------------------------------------------------------------|
| [Manifest schema](./manifest-schema/)                        | Every field in `apm.yml`                                         |
| [Lockfile spec](./lockfile-spec/)                            | `apm.lock.yaml` schema, semantics, and integrity contract        |
| [Policy schema](./policy-schema/)                            | `apm-policy.yml` rules, merge semantics, severity                |
| [Registry HTTP API](./registry-http-api/)                    | Wire contract for APM registry servers: endpoints, auth, errors  |
| [Targets matrix](./targets-matrix/)                          | Which primitive types each target (Copilot, Claude, ...) supports |
| [MCP variable compatibility](./mcp-variable-compatibility/)  | How each harness reads env references in MCP config, and what APM writes |
| [Baseline checks](./baseline-checks/)                        | The CI checks `apm audit --ci` runs                              |
| [Environment variables](./environment-variables/)            | Every env var APM reads, with precedence                         |
| [Primitive types](./primitive-types/)                        | Skill, prompt, instruction, agent, hook, command                 |
| [Package types](./package-types/)                            | APM packages, plugins, marketplaces                              |
| [Examples](../quickstart/)                                      | Worked end-to-end examples                                       |
| [Experimental](./experimental/)                              | Surface area still subject to change                             |
| [Common errors](../troubleshooting/common-errors/)                            | Symptoms, causes, and fixes for frequently reported errors       |

## Conventions used in reference pages

- `<placeholder>` -- you must substitute a real value.
- `[optional]` -- argument or flag is optional.
- All commands assume `apm` is on `PATH`; install instructions are in the [Quickstart](../quickstart).
- Status symbols in CLI output: `[+]` success, `[!]` warning, `[x]` error, `[i]` info, `[*]` action, `[>]` running.
