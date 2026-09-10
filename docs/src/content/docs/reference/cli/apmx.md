---
title: apmx
description: Run one local or packaged contract on native Copilot and retain its assessed output.
sidebar:
  order: 12
---

Bundled with APM, `apmx` runs one explicit contract on one harness, captures one
output, runs independent checks, and retains a record.

:::caution[Experimental]
Enable contract planning and execution first:

```bash
apm experimental enable contracts
```
:::

## Synopsis

```bash
apmx CONTRACT --on copilot [--model MODEL] --allow-advisory [-v]
apmx --from PACKAGE_REF contracts/file.contract.md --on copilot [--model MODEL] --allow-advisory [-v]
apmx [--from PACKAGE_REF] CONTRACT --on copilot --plan [--model MODEL]
```

| Option | Description |
|---|---|
| `--from PACKAGE_REF` | Local APM directory or ordinary Git reference, including subdirectories and existing `#ref` syntax. |
| `--on copilot` | Required; the only supported harness. |
| `--model MODEL` | Request a model; recorded separately from the observed model. |
| `--allow-advisory` | Required for execution in terminals and pipes; accepts native-host limits, not a policy override. |
| `--plan` | Inspect local or valid installed sources offline/read-only. Unresolved remote sources fail explicitly with `UNPROVEN` / `21`. |
| `-v, --verbose` | Show detailed output. |
| `--help`, `--version` | Show usage or version, including on Windows. Contract execution requires macOS/Linux. |

There is no `--param` or default-contract fallback. [`apm run`](../run/) retains
its script semantics and explicit local contract compatibility.

## Sources, inputs, and retained output

Run from the **caller directory** containing required inputs.
[Standalone local contracts](../plan/#contract-source) need no `apm.yml` unless
importing a skill. `--from` requires a package-relative `.contract.md` and the
package's own valid `apm.yml`.

A valid `apm.yml`, selected contract, and required `checks/` resources can form
a contract-only source; `.apm/` primitives are not required.
Ordinary [`apm install`](../install/) package requirements are unchanged.

For Git sources, a matching direct caller declaration and lock entry bind
planning and execution to the same verified locked identity. Execution reuses
a valid installed source or acquires the locked commit and verifies its hash;
it never follows a moved mutable ref or repairs an invalid lock.
The caller's lock stays unchanged.

| Content | Selected from |
|---|---|
| `needs` | Caller directory, never the package's sample inputs |
| Contract and `checks/**` | Caller in local mode; selected package with `--from` |
| Declared skill import | Selected source's manifest and dependency identity |
| Artifact and `record.json` | Caller's `.apm/runs/<run-id>/` |

Package mode refuses nonempty caller `checks/` trees and conflicting capture
paths. Keep helpers under `checks/`. Marketplace, registry,
virtual-file packages, and inherited parent-repository manifests are unsupported.

`--from` execution may fetch the package and one declared self-contained skill privately.
It leaves caller and source manifests, locks, installed context, and global
configuration unchanged.
No general dependency graph, companion skill resources, transitive imports,
MCP, hook, or plugin activation is supported. See
[import eligibility](../plan/#imported-skill-context).

Artifacts stay in `.apm/runs/<run-id>/artifacts/`, without automatic copy-back.
The record identifies sources, inputs, artifact, checks, and execution details.
Private logs are not guaranteed secret-free or safe to publish.

## Native execution boundary

- **Host access, not a sandbox.** Copilot and checks run as you and may access
  host files, network, and ambient credentials. Native extensions remain outside
  confinement; `apmx` does not guarantee a clean user profile.
- **No-policy callers only.** Use independent, secret-free local fixtures with
  no Git remote or configured policy requirement. Governed, unknown, or disabled
  discovery and `APM_NO_SCRIPTS` refuse. Never remove remotes or policy to bypass
  refusal. Package location does not change caller eligibility.
- **Host prerequisites.** Native Copilot must be ready on `PATH` with model
  access, plus Git and the check's executables.
- **Bounded waiting.** The attempt watchdog is 1,200 seconds; each check gets
  at most 180 seconds, clipped to remaining time. Cleanup targets the original
  process group for at most six seconds, not escaped descendants.

Execution inventories Copilot MCP server names through `copilot mcp list --json`
within ten seconds and disables reported servers per invocation. An unavailable
or invalid inventory halts before generation; planning never probes it. The
producer receives `view` and `apply_patch`, an output-file write grant, and
`--no-bash-env`; shell and URL tools are denied. These controls are advisory,
not isolation.

From your caller directory, use an invocation-local fresh profile for testing:

```bash
COPILOT_HOME="$(mktemp -d)" apmx ./handoff.contract.md \
  --on copilot --model gpt-6-astra --allow-advisory
```

It still needs authentication and model access. This separates native user
context without global changes; it is not a sandbox or the default.

## Independent checks

APM captures tracked working bytes, including dirty changes, plus selected
untracked files. Non-Git callers supply only explicit selections.
Inputs and artifacts retain exact bytes.

The producer starts without the declared output. Each check gets a fresh
baseline, the same frozen artifact, and pre-generation `checks/**`, never
producer-edited helpers or another check's workspace. Checks must apply patch
artifacts themselves; the engine never pre-applies them.

## Results and retained files

Check exits `0`, `1`, and `2` mean pass, failure, and incomplete. Unknown exits,
missing tools, signals, invalid identities, and per-check timeouts are incomplete.
An attempt timeout or lingering check child instead halts the run.
Outcomes apply in this order:

| Code | Outcome | Meaning |
|---|---|---|
| `22` | `HALTED` | Operational stop, cancellation, watchdog, capture-integrity, cleanup, or final-recording failure. |
| `20` | `REJECTED` | A substantive check fails, even if another is incomplete. |
| `21` | `UNPROVEN` | Missing artifact, required incomplete check, or unavailable consent/assurance, with no preceding outcome. |
| `0` | `VERIFIED` | Fresh artifact, all checks pass, accepted advisory controls, and completed record. |

CLI usage errors exit `2`. A refusal before admission need not create a run;
a nonterminal record means incomplete or unknown, not permission to replay.
Credential-bearing URL text is redacted from retained package references.
Transcript or record-write failures report finalization failure and retain an
incomplete `HALTED` record when the filesystem still permits writing.

`VERIFIED` / `0` means native-advisory check success, not factual correctness,
sandboxing, signing, budget enforcement, or merge permission. There is no
retry/resume, graph scheduling, or delivery facility.

Contract checks do not replace package security: **built-in protection**
automatically blocks critical findings during `install`, `compile`, and `unpack`,
with zero configuration. **`apm audit`** provides explicit reporting
(SARIF/JSON/markdown), remediation (`--strip`), and standalone scanning (`--file`).
See [the two-layer model](../../../enterprise/security/).
