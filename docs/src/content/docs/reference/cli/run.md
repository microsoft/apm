---
title: apm run
description: Execute a script or explicitly run a local contract on native Copilot.
sidebar:
  order: 12
---

Execute a shell command from `apm.yml` `scripts:`, npm-style, or explicitly
select a local contract with `--on copilot`.

:::caution[Experimental]
Contract mode is disabled by default. Enable it with
`apm experimental enable contracts`. Existing script execution does not require
the flag.
:::

## Synopsis

```bash
apm run [SCRIPT_NAME] [OPTIONS]
apm run CONTRACT --on copilot [--model MODEL] --allow-host-access [-v]
```

Without `--on`, omitting `SCRIPT_NAME` runs `start`; if absent, APM exits
non-zero and lists scripts. All legacy script and prompt behavior below
remains unchanged, even for script names ending in `.contract.md`.

## Description

Without `--on`, APM resolves `SCRIPT_NAME` against `scripts:` and runs the
matching shell command. It first compiles referenced `.prompt.md` files,
substituting `${input:name}` from `--param` and writing
`.apm/compiled/<name>.txt`.

If `SCRIPT_NAME` does not match a script, APM falls back to:

1. Auto-discovering a matching prompt file in `.apm/prompts/`, `.github/prompts/`, or the project root.
2. Auto-installing a virtual package reference (e.g., `owner/repo/path/to/prompt`) and re-running the discovery step.

If none of these resolve, the command exits non-zero with an error listing the available scripts.

## Options

| Option | Description |
|---|---|
| `-p, --param NAME=VALUE` | Set a parameter for prompt compilation. Repeat for multiple parameters. |
| `-v, --verbose` | Show detailed compilation and execution output. |
| `--on copilot` | Select contract mode; require a local `.contract.md`, without script/prompt discovery or installation. |
| `--model MODEL` | Request a native model; requires `--on`. |
| `--allow-host-access` | Allow Copilot and checks to use host files, network and available login details for this run; requires `--on`. |
| `--help` | Show help for the command. |

`--param` is rejected in contract mode. `--model` and `--allow-host-access` cannot
alter legacy scripts. There is no command-level `--json` flag.

## Examples

Define scripts in `apm.yml` (npm-style):

```yaml
name: hello-world-agent
version: 0.0.1

scripts:
  start: copilot -p hello-world.prompt.md --allow-all-tools
  claude: claude -p hello-world.prompt.md
  codex: codex exec --skip-git-repo-check hello-world.prompt.md
  llm: llm < hello-world.prompt.md

dependencies:
  apm:
    - dmeppiel/hello-world
```

Run the default `start` script:

```bash
apm run
```

Run a named script:

```bash
apm run claude
apm run codex
```

Pass parameters that get substituted into `${input:name}` placeholders inside `.prompt.md` files:

```bash
apm run start --param name="Alice"
apm run llm --param service=api --param environment=prod
```

List available scripts (no script defined and no `start`):

```bash
$ apm run
[x] No script specified and no 'start' script defined in apm.yml
[>] Available scripts:
   claude   claude -p hello-world.prompt.md
   codex    codex exec --skip-git-repo-check hello-world.prompt.md
   llm      llm < hello-world.prompt.md
```

## Argument forwarding

Scripts have no `--` argument passthrough. Use `--param NAME=VALUE` with
`${input:name}` in a `.prompt.md` file:

```markdown
Hello, ${input:name}. Today's target service is ${input:service}.
```

Then run:

```bash
apm run start --param name="Alice" --param service=api
```

Other parameterization belongs in the shell script body.

## Script exit codes

| Code | Meaning |
|---|---|
| `0` | Script executed successfully. |
| `1` | Script failed, was not found, or no `start` script is defined when invoked without arguments. |

## Contract execution

The explicit local leaf remains supported:

```bash
apm run ./handoff.contract.md --on copilot --model gpt-6-astra --allow-host-access
```

Use [`apmx`](../apmx/) for package-selected contracts. Both entrypoints use the
same [source format](../plan/#contract-source) and bounded execution engine.

### Native execution boundary

The shared [native-host limits, consent, and caller policy requirements](../apmx/#native-execution-boundary)
apply. Contract mode is not a sandbox and supports only Copilot on macOS/Linux.

### Captured inputs and checks

See [independent checks](../apmx/#independent-checks) for baseline capture and
frozen-artifact assessment.

### Results and retained files

See [outcomes and retained evidence](../apmx/#results-and-retained-files).
Artifacts stay under the caller's `.apm/runs/`, without automatic copy-back.
Native contract runs return `UNPROVEN` / `21` even when all checks pass,
because host isolation is not enforced. Script exit codes above are unchanged.

## Related

- [`apmx`](../apmx/) -- run one local or packaged contract.
- [`apm plan`](../plan/) -- inspect a contract without execution or durable writes.
- [`apm list`](../list/) -- show installed primitives and available scripts.
- [`apm preview`](../preview/) -- render the compiled command and prompt files without executing.
