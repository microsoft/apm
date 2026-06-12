---
title: apm preview
description: Show a script's compiled prompt and final command without executing it.
sidebar:
  order: 13
---

## Synopsis

```bash
apm preview [SCRIPT_NAME] [OPTIONS]
```

## Description

`apm preview` resolves a script defined in the `scripts:` section of `apm.yml`, compiles any `.prompt.md` files referenced by its command, and prints both the original command and the compiled command without running the runtime. Use it to verify parameter substitution, frontmatter handling, and the resulting argv before you spend tokens on `apm run`.

If `SCRIPT_NAME` is omitted, `apm preview` falls back to the `start` script. If no script name is given and no `start` entry exists in `apm.yml`, the command exits `1`.

Only files whose name ends in `.prompt.md` are compiled. Any other path in the script command is shown as-is and a warning notes that no compilation occurred.

## Options

| Flag | Default | Description |
|---|---|---|
| `--param`, `-p NAME=VALUE` | none | Parameter to substitute into the prompt. Repeatable. Values lacking `=` are silently ignored. |
| `--verbose`, `-v` | off | Print each parsed parameter and extra diagnostic context. |

## Arguments

| Argument | Required | Description |
|---|---|---|
| `SCRIPT_NAME` | no | Name of a script defined under `scripts:` in `apm.yml`. Defaults to `start` when omitted. |

## Behavior

- **Compilation target.** Compiled prompts are written to `.apm/compiled/<basename>.txt` (the `.prompt.md` suffix is stripped). The same files would be produced by `apm run`.
- **Output panels.** When the rich renderer is available, `apm preview` prints three panels: the original command, the compiled command, and the list of compiled prompt file paths. With no `.prompt.md` in the command, it prints a single yellow panel and a warning instead.
- **No runtime invocation.** The script's runtime (`codex`, `llm`, `claude`, etc.) is never spawned. Preview only resolves and compiles; it does not execute.
- **Parameter parsing.** `--param foo=bar` becomes `{foo: "bar"}` and is passed to the prompt compiler. The first `=` splits the key from the value; later `=` characters stay in the value. A `--param` with no `=` is dropped.

## Examples

### Preview the default `start` script

```bash
apm preview
```

### Preview a named script

```bash
apm preview llm
```

### Substitute parameters

```bash
apm preview start --param name=Alice
apm preview review -p reviewer=Bob -p depth=full
```

### See parsed params and full diagnostics

```bash
apm preview start -p name=Alice --verbose
```

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Preview rendered successfully. |
| `1` | No script name given and no `start` script defined, the named script is not in `apm.yml`, or an unhandled error occurred during compilation. |

## Related

- [`apm run`](../run/) -- execute the same script after previewing.
- [`apm list`](../list/) -- list every script defined in `apm.yml`.
- [`apm compile`](../compile/) -- compile primitives into `AGENTS.md` and harness files (different surface from script-prompt compilation).
- [Manifest schema](../../manifest-schema/) -- field reference for `scripts:` in `apm.yml`.
