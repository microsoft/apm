---
title: apm run
description: Execute a script defined in apm.yml
sidebar:
  order: 12
---

Execute a script defined in the `scripts:` section of `apm.yml`. Modeled on `npm run`: script bodies are shell commands, typically a prompt piped to a runtime CLI (Copilot, Claude, Codex, llm, etc.).

:::caution[Experimental]
The `run` command surface is marked experimental. Flags and behavior may change before 1.0.
:::

## Synopsis

```bash
apm run [SCRIPT_NAME] [OPTIONS]
```

If `SCRIPT_NAME` is omitted, APM runs the `start` script. If no `start` script is defined, APM exits non-zero and prints the available scripts.

## Description

`apm run` resolves `SCRIPT_NAME` against `apm.yml` `scripts:` and executes the matching shell command. Before execution, APM auto-compiles any `.prompt.md` file referenced in the command, substituting `${input:name}` placeholders with values from `--param`. Compiled output is written to `.apm/compiled/<name>.txt` and the final command is executed in the current shell.

If `SCRIPT_NAME` does not match a script, APM falls back to:

1. Auto-discovering a matching prompt file in `.apm/prompts/`, `.github/prompts/`, or the project root.
2. Auto-installing a virtual package reference (e.g., `owner/repo/path/to/prompt`) and re-running the discovery step.

If none of these resolve, the command exits non-zero with an error listing the available scripts.

## Options

| Option | Description |
|---|---|
| `-p, --param NAME=VALUE` | Set a parameter for prompt compilation. Repeat for multiple parameters. |
| `-v, --verbose` | Show detailed compilation and execution output. |
| `--help` | Show help for the command. |

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

`apm run` does not forward extra positional arguments to the underlying script (there is no `--` passthrough). To parameterize a script, use `--param NAME=VALUE` and reference the value inside your `.prompt.md` file:

```markdown
Hello, ${input:name}. Today's target service is ${input:service}.
```

Then run:

```bash
apm run start --param name="Alice" --param service=api
```

Anything beyond `--param`-style substitution belongs in the script body itself, which is plain shell.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Script executed successfully. |
| `1` | Script failed, was not found, or no `start` script is defined when invoked without arguments. |

## Related

- [`apm list`](../list/) -- show installed primitives and available scripts.
- [`apm preview`](../preview/) -- render the compiled command and prompt files without executing.
