---
title: Preview and validate
description: The local verify-before-ship loop. Inspect, list, audit, and dry-run before you pack.
---

Before you pack and publish, run the verify loop locally. APM ships
five read-only commands that answer the questions a producer needs to
answer before shipping: what would deploy, what is installed, what is
stale, and what is unsafe.

```bash
apm compile --dry-run     # what would be written, without writing
apm view <your-package>   # metadata for an installed package
apm list                  # scripts in your apm.yml
apm outdated              # which deps drift behind their refs
apm audit                 # hidden Unicode + drift against the lockfile
```

The recommended sequence is `compile --dry-run` -> `apm view` ->
`apm audit` -> [`apm pack`](../pack-a-bundle/). Run it once at the end
of every authoring session.

## apm compile --dry-run

```bash
apm compile --dry-run
apm compile --dry-run --target claude,cursor
apm compile --validate
```

Prints the placement decisions [`apm compile`](../compile/) would make
without touching disk: which primitives go to which harness directory,
which files would be overwritten, which targets would be skipped. Use
it whenever you are not sure what your `apm.yml` and `.apm/` will
produce.

`--validate` is the stricter sibling. It parses every primitive's
frontmatter and structure and reports errors without producing output.
Run it before `--dry-run` when you have just edited a primitive and
want a quick syntax check.

Pair `--dry-run` with `--target` to scope the preview to one harness.
Pair it with `--verbose` to see source attribution per file.

## apm view

```bash
apm view <your-package>            # local metadata
apm view <your-package> versions   # remote tags and branches
```

`apm view` reads from `apm_modules/` and reports the package's name,
version, description, author, source, locked ref and commit, install
path, primitive counts (instructions, prompts, skills, agents), and
hook count. Use it after installing your package into a fresh test
project to confirm what consumers will see.

`apm view <package> versions` queries the remote and lists available
tags and branches. Useful before publishing a new tag to confirm what
is already out there.

:::note
`apm view` requires the package to be installed. To preview your own
in-development package, install it from a local path first:
`apm install ./path/to/your/package` in a scratch project, then
`apm view <name>`.
:::

## apm list

```bash
apm list
```

Lists the scripts declared in your project's `apm.yml`. This is the
fast way to confirm your `scripts:` block is well-formed and your
`start` default points at the right command.

For a tree of installed dependencies (versions, sources, primitive
counts), use `apm deps list` instead. See
[CLI reference](../../reference/cli/install/) for both.

## apm outdated

```bash
apm outdated
apm outdated --verbose
apm outdated -j 8
```

Compares every locked dependency against its remote and reports which
ones drift behind. Tag-pinned deps use semver comparison; branch-pinned
deps compare commit SHAs. Local paths and Artifactory deps are skipped.

Run it before publishing. Shipping a package whose own dependencies
are months stale is the kind of small thing that erodes consumer
trust. `--verbose` prints the available tags so you can decide whether
to bump.

## apm audit

```bash
apm audit                # scan deployed files + drift check
apm audit --file <path>  # scan one arbitrary file
apm audit --strip        # remove dangerous hidden characters
apm audit --strip --dry-run   # preview the strip
```

`apm audit` is the producer's last gate before pack. It scans every
deployed prompt, instruction, skill, and agent file for hidden Unicode
(zero-width characters, bidi controls, tag characters), then rebuilds
the deployed context in a scratch directory and diffs it against your
working tree to catch hand-edits to `apm_modules/` or generated files.

Findings come back as text by default. Use `-f json`, `-f sarif`, or
`-f markdown` for machine-readable output, and `-o <path>` to write to
file.

For the conceptual model behind hidden-Unicode threats and the
secure-by-default layer, see
[Drift and secure-by-default](../../consumer/drift-and-secure-by-default/).

## The recommended verify sequence

```bash
apm compile --validate         # 1. structure check
apm compile --dry-run          # 2. preview placement
apm view <your-package>        # 3. confirm metadata
apm outdated                   # 4. check dep freshness
apm audit                      # 5. scan + drift
apm pack                       # 6. ship it
```

Steps 1-5 are read-only and safe to run in any order. Step 6 produces
the bundle: see [Pack a bundle](../pack-a-bundle/) for the next step,
then [Publish to a marketplace](../publish-to-a-marketplace/) to ship
it.
