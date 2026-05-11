---
title: Run scripts
description: Execute named scripts from apm.yml on any AI runtime, npm-style.
---

:::caution[Experimental surface]
`apm run` is an experimental script runner -- think `npx` for AI
runtime CLIs. It is optional and off the critical path: every package
you install is already wired into your detected harnesses without it.
Use it when you want a single, reproducible entrypoint to invoke a
runtime CLI against an installed prompt; skip it otherwise.
:::

After `apm install` wires primitives into your harnesses, `apm run` executes
a named script declared in `apm.yml`. This is APM's npm-scripts surface for
AI runtimes -- one manifest, any runtime CLI you have on your `PATH`.

This page covers the consumer side: running scripts that already exist in a
project. For authoring scripts and prompts, see the producer ramp.

## The `scripts:` block

`apm.yml` carries a flat string-to-string `scripts:` mapping, mirroring
`package.json`:

```yaml
name: my-project
version: 0.1.0

scripts:
  start: "copilot --log-level all --allow-all-tools -p hello-world.prompt.md"
  codex: "codex --skip-git-repo-check hello-world.prompt.md"
  llm:   "llm hello-world.prompt.md -m github/gpt-4o-mini"
```

Each value is a literal shell command. The canonical pattern is shelling
out to a runtime CLI -- `copilot`, `claude`, `codex`, `cursor-agent`,
`gemini`, `opencode`, `windsurf`, or `llm` -- with a prompt file argument.
APM does not bundle these runtimes; you install them yourself and APM
invokes whichever the script names.

Object-form entries (with `description`, `env`, ...) are not supported.
Keep values as strings.

## Running a named script

```bash
apm run codex
apm run review --param target=src/auth.py
```

`apm run <name>` looks up `<name>` in the `scripts:` block and executes
the literal command. Pass parameters with `--param key=value` (repeatable,
short form `-p`); they are interpolated into any `.prompt.md` files the
command references, not exported as shell environment variables.

Before launching the command, APM auto-compiles any `.prompt.md` argument
in the command line into `.apm/compiled/<name>.txt` and rewrites the
argument to the compiled path. Commands that reference no `.prompt.md`
file run as-is.

To see exactly what would run without executing it, use `apm preview
<name>` -- it prints the original command, the rewritten command, and the
list of compiled prompt files.

## Running with no arguments

```bash
apm run
```

With no script name, `apm run` runs the literal `start` script. It does
not auto-discover prompts and it does not pick a "default" runtime.

If `start` is not defined, the command exits with status 1 after printing
the available scripts:

```
[x] No script specified and no 'start' script defined in apm.yml
[i] Available scripts:
  codex   codex --skip-git-repo-check hello-world.prompt.md
  llm     llm hello-world.prompt.md -m github/gpt-4o-mini
```

Define a `start` entry pointing at your preferred runtime to make bare
`apm run` work.

## One script per runtime: the portable pattern

The third promise -- run anywhere -- shows up here. A package that wants
to be portable defines one script per runtime, all pointing at the same
prompt:

```yaml
scripts:
  start: "copilot -p review.prompt.md"
  claude: "claude -p review.prompt.md"
  codex:  "codex review.prompt.md"
  llm:    "llm review.prompt.md -m github/gpt-4o-mini"
```

A consumer with `copilot` on their `PATH` runs `apm run`. A consumer on
`codex` runs `apm run codex`. The prompt file is the same; only the
runtime CLI differs. APM compiles the `.prompt.md` once per invocation
and hands the compiled text to whichever CLI the script names.

See [primitives and targets](../../concepts/primitives-and-targets/) for
how a single prompt reaches every harness, and the [agent workflows
guide](../../guides/agent-workflows/) for end-to-end script patterns.

:::note[Coming from npm?]
The `scripts:` shape is intentionally identical to `package.json`. Bare
`apm run` runs `start`, just like `npm start`. The difference: APM runs
the prompt compiler in front of your command, so you can keep prompts in
their authoring format and let the runtime see compiled text.
:::

## Common surprises

- The runtime CLI must be on your `PATH`. APM does not install `copilot`,
  `claude`, `codex`, or any other harness binary.
- `--param` values reach prompt frontmatter, not the shell. To pass a
  shell variable, embed it in the script string itself.
- Auto-compilation is keyed on the `.prompt.md` extension. A `.md` file
  that is not a prompt is passed through unchanged.
- `apm run` does not re-run the install-time security scan. If you
  hand-edit primitives between installs, run `apm audit` -- see the
  [lifecycle](../../concepts/lifecycle/) page.

## Read more

- Install primitives first: [install packages](../install-packages/)
- Inspect what will execute: `apm preview` in the [CLI reference](../../reference/cli/install/)
- The portability promise: [the three promises](../../concepts/the-three-promises/)
