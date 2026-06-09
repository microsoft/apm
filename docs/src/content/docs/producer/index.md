---
title: Author and publish APM packages
description: Build, validate, pack, and publish APM packages others can install.
sidebar:
  order: 0
---

You're here because you want to *author* an APM package others can install. This is the producer ramp.

Start a fresh package with [`apm plugin init`](../reference/cli/plugin/); add a marketplace later with [`apm marketplace init`](../reference/cli/marketplace/). Two primitive postures cover most repos -- ship your own plugin, or curate others into a marketplace -- and they compose: one repo can do both at once. The five steps below tell you what to do between scaffolding and shipping.

## The producer ladder

Five steps, in order. Each links to the page that owns it:

| # | Step                            | What success looks like                                                          |
|---|---------------------------------|----------------------------------------------------------------------------------|
| 1 | [Author primitives](./author-primitives/) | Skills, prompts, instructions, agents, hooks, commands, MCP under `.apm/`        |
| 2 | [Compile your package](./compile/)        | `apm compile` writes deterministic per-target output you can git-diff            |
| 3 | [Preview and validate](./preview-and-validate/) | `apm preview` and `apm view` confirm what consumers will receive                 |
| 4 | [Pack a bundle](./pack-a-bundle/)         | `apm pack` produces a bundle you can ship offline or to a marketplace         |
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

## Production-grade releases

Once the 5-rung ladder works end to end, three pages cover the operational concerns of shipping at scale. They are independent of each other -- pick what you need.

| Concern                                  | Page                                                                 |
|------------------------------------------|----------------------------------------------------------------------|
| Picking a repo layout before you author  | [Repo shapes](./repo-shapes/) -- two starting layouts plus a hybrid composition for teams that ship their own plugin alongside a curated marketplace of others |
| Aligning versions across local packages  | [Versioning strategies](./versioning-strategies/)                    |
| Wiring the release into any CI provider  | [Releasing from any CI](./releasing-from-any-ci/)                    |

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

`apm compile` deterministically transforms `.apm/` into per-target output. `apm pack` zips the result and, when an `apm.yml` declares a `marketplace:` block, writes the marketplace artifact alongside the bundle. Consumers reach it with `apm marketplace add`. There is no separate "build pipeline" -- the CLI is the build pipeline.

## Compatible with the consumer ramp

Every package you publish here installs through the [consumer ramp's](../consumer/) `apm install` command. Test that loop yourself: install your own package in a scratch repo before declaring it shipped. The [preview](./preview-and-validate/) page walks you through the dry-run.
