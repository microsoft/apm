---
title: Author and publish APM packages
description: Build, validate, pack, and publish APM packages others can install.
sidebar:
  order: 0
---

You're here because you want to *author* an APM package others can install. This is the producer ramp.

## The producer ladder

Five steps, in order. Each links to the page that owns it:

| # | Step                            | What success looks like                                                          |
|---|---------------------------------|----------------------------------------------------------------------------------|
| 1 | [Author primitives](./author-primitives/) | Skills, prompts, instructions, agents, hooks, commands, MCP under `.apm/`        |
| 2 | [Compile your package](./compile/)        | `apm compile` writes deterministic per-target output you can git-diff            |
| 3 | [Preview and validate](./preview-and-validate/) | `apm preview` and `apm view` confirm what consumers will receive                 |
| 4 | [Pack a bundle](./pack-a-bundle/)         | `apm pack` produces a `.tar.gz` you can ship offline or to a marketplace         |
| 5 | [Publish to a marketplace](./publish-to-a-marketplace/) | Others install your package with `apm install <owner>/<repo>`                  |

You don't need a marketplace to start. Step 4 is enough for internal teams; the marketplace step is for public discovery.

## Where to start

| Your situation                                                       | Start here                                                  |
|----------------------------------------------------------------------|-------------------------------------------------------------|
| First time -- want a working package end-to-end                      | [Your first package](../getting-started/first-package/)     |
| You have primitives in `.apm/` and need to test them locally         | [Compile your package](./compile/)                          |
| You're about to ship -- want to verify what consumers will see       | [Preview and validate](./preview-and-validate/)             |
| You need to ship a single file to a customer / air-gapped env        | [Pack a bundle](./pack-a-bundle/)                           |
| You want a public marketplace listing                                | [Publish to a marketplace](./publish-to-a-marketplace/)     |
| Your package links into other packages and you hit broken refs       | [Package-relative links](./package-relative-links/)         |

## The producer mental model

A producer package is just a directory with:

```text
my-package/
  apm.yml                    # manifest -- who you are, what you depend on
  .apm/
    skills/                  # primitives consumers can install
    prompts/
    instructions/
    agents/
    hooks/
  README.md                  # rendered on the marketplace listing
```

`apm compile` deterministically transforms `.apm/` into per-target output. `apm pack` zips the result. `apm publish` (via a marketplace adapter) lists it. There is no separate "build pipeline" -- the CLI is the build pipeline.

## Compatible with the consumer ramp

Every package you publish here installs through the [consumer ramp's](../consumer/) `apm install` command. Test that loop yourself: install your own package in a scratch repo before declaring it shipped. The [preview](./preview-and-validate/) page walks you through the dry-run.
