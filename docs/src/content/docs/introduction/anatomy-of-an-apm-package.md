---
title: "Anatomy of an APM Package"
description: "What .apm/ is, why it exists, and how APM decides what is importable."
sidebar:
  order: 5
---

If you have read [What is APM?](./what-is-apm/) and [How It Works](./how-it-works/),
you know APM is a package manager for agent primitives. This page answers the
next question every user asks: what does an APM package actually look like on
disk, and why does it look that way?

## The one-line mental model

`apm.yml` is your `package.json`. `.apm/` is your `src/`. `apm_modules/` is your
`node_modules/`. The compiled output under `.github/`, `.claude/`, `.cursor/`,
and friends is your `dist/` -- generated, tool-specific, not the source of
truth.

If you remember nothing else: **`.apm/` holds the primitives you author.
Everything outside `.apm/` that looks similar is either a build artifact or
someone else's package.**

## Why `.apm/` exists

AI coding tools each invented their own folder for context: `.github/` for
Copilot, `.claude/` for Claude Code, `.cursor/rules/` for Cursor, and so on.
Each one is read at runtime by exactly one tool. None of them are designed to
be authored portably, versioned as a dependency, or shared across tools.

APM separates two concerns that those folders conflate:

1. **Source primitives** -- the skills, agents, instructions, and prompts you
   write and version. These live in `.apm/`.
2. **Compiled output** -- the tool-specific files APM generates from your
   sources for each runtime you target. These live in `.github/`, `.claude/`,
   `.cursor/`, etc.

`apm install` and `apm compile` read from `.apm/` and write outward.

### A concrete example: this repo

The `microsoft/apm` repository (the one shipping the CLI you are reading docs
for) dogfoods this layout. It contains both source and compiled output side
by side:

```
microsoft/apm/
+-- apm.yml
+-- .apm/
|   +-- skills/
|   |   +-- python-architecture/
|   |       +-- SKILL.md
|   +-- agents/
|   |   +-- doc-writer.agent.md
|   +-- instructions/
+-- .github/
|   +-- skills/
|   |   +-- python-architecture/
|   |       +-- SKILL.md         (deployed from .apm/ by apm install)
|   +-- agents/
|   |   +-- doc-writer.agent.md
|   +-- instructions/
+-- src/
+-- tests/
```

The source files under `.apm/` are authoritative. You can inspect them on
GitHub:
[`.apm/skills/python-architecture/SKILL.md`](https://github.com/microsoft/apm/blob/main/.apm/skills/python-architecture/SKILL.md)
and
[`.apm/agents/doc-writer.agent.md`](https://github.com/microsoft/apm/blob/main/.apm/agents/doc-writer.agent.md).
Their counterparts under `.github/` are the deployed copies the in-repo
Copilot agent actually loads while we work on the CLI.

For simple primitives the deployed file is byte-identical to the source.
The deploy step can also augment files for runtime-specific concerns (e.g.
adding diagnostic guidance for a particular target), so treat `.github/`
as build output: never edit it by hand, always re-deploy from `.apm/`.

## Why not just put primitives in `.github/` directly?

It is tempting. `.github/` already exists, Copilot already reads it, why add
another folder?

Three reasons, in order of severity.

**1. Self-referential context pollution.**
The Copilot, Claude, or Cursor agent helping you author a skill reads
whatever sits in its runtime folder. If you author skills directly into
`.github/skills/`, your in-progress, half-written, possibly broken skill
becomes part of the system prompt of the agent you are using to write it.
Writing a code-review skill? Copilot starts applying it -- including to the
skill file itself -- before you have finished. Keeping sources in `.apm/`
means the dev-time agent only sees what you have explicitly compiled.

**2. Portability across runtimes.**
A skill in `.github/skills/` is a Copilot-shaped file. A skill in
`.claude/skills/` is a Claude-shaped file. They are not interchangeable. The
whole point of APM is one source, many runtimes. That requires a
runtime-neutral source folder, and `.github/` is not it.

**3. Packaging boundary.**
`apm pack` needs to know what is part of the package and what is incidental.
A dedicated `.apm/` directory makes that boundary trivial. Mixing sources
into `.github/` makes it a guessing game.

## Why not the repo root?

Also tempting, also wrong, for symmetric reasons:

- **Naming collisions.** Most repos already have `skills/`, `agents/`, or
  `prompts/` directories that mean something else (test fixtures, app code,
  marketing copy). APM cannot safely claim those names at the root.
- **No discoverability signal.** A consumer cloning your repo cannot tell at
  a glance whether it is an APM package. `.apm/` plus `apm.yml` is that
  signal.
- **No clean pack boundary.** Same problem as `.github/`: `apm pack` would
  need heuristics to know what to bundle.

`.apm/` is short, namespaced, conventional, and unambiguous. That is the
whole argument.

## Why not just ship a `plugin.json`?

This is the sharpest version of the question, because plugin formats are
real and the ecosystem is converging on them. APM does not compete with
plugins -- it sits underneath them.

- `plugin.json` is a **runtime distribution format**. It tells a single
  host (Copilot CLI, Claude Code, Cursor) how to load a bundle of
  primitives at runtime.
- `.apm/` is a **source layout**. It tells APM what you authored, so it
  can resolve dependencies, lock versions, scan for security issues, and
  compile to *every* runtime -- including plugin format.

The two are complementary, and APM treats them that way:

1. **APM consumes plugins as first-class dependencies.** Any repo with a
   `plugin.json` (root, `.github/plugin/`, `.claude-plugin/`, or
   `.cursor-plugin/`) is auto-recognized by `apm install`. APM
   synthesizes an `apm.yml` from the plugin metadata so it gets version
   pinning, lockfile entries, and transitive resolution. Marketplaces
   (`marketplace.json`) resolve through the same path. See
   [Plugins](../../guides/plugins/) and [Marketplaces](../../guides/marketplaces/).
2. **APM compiles `.apm/` to plugin format.** Run `apm pack --format
   plugin` and you get a standalone plugin directory -- no `apm.yml`, no
   `apm_modules/`, no `.apm/` -- consumable by any plugin host. See
   [Pack & Distribute -- Plugin format](../../guides/pack-distribute/#plugin-format).
3. **Hybrid mode is supported.** A repo can ship `apm.yml` + `plugin.json`
   together: author with APM (dependency management, lockfile, security
   scanning, dev/prod separation), distribute as a standard plugin.

What `plugin.json` alone does not give you: transitive dependency
resolution, a consumer-side lockfile, security scanning that blocks
critical findings on install, `devDependencies` that stay out of the
shipped artifact, or a single source that targets multiple runtimes.
That is the gap `.apm/` fills. If you only ever target one host and
never depend on shared primitives, plugin-only is fine -- and APM still
consumes you.

## Two ways to be importable

A repo can expose primitives to APM consumers in two forms. They are not
mutually exclusive.

### Package form

The repo declares itself an APM package: `apm.yml` at the root, primitives
under `.apm/`. Consumers reference it by repo name:

```yaml
# consumer's apm.yml
dependencies:
  apm:
    - your-org/your-repo
```

`apm install` resolves the repo, reads its `apm.yml`, and pulls every
primitive declared in `.apm/` into `apm_modules/`.

This is the right form when:

- You are publishing a curated set of primitives meant to be consumed
  together.
- You want a one-line install for the whole bundle.
- You want versioning, lockfile entries, and a clean update path.

Canonical examples: [`microsoft/apm-sample-package`](https://github.com/microsoft/apm-sample-package),
[`apm-handbook`](https://github.com/danielmeppiel/apm-handbook) (a multi-package
monorepo with `apm.yml` plus `.apm/skills/` and `.apm/agents/`), and this
repository itself.

### Primitive form

Any subdirectory of any GitHub repo that looks like a primitive can be
imported directly by path. The upstream repo does not need an `apm.yml` and
does not need to use `.apm/`:

```yaml
# consumer's apm.yml
dependencies:
  apm:
    - github/awesome-copilot/skills/review-and-refactor
```

APM treats the subdirectory as a virtual single-primitive package.

This is the right form when:

- You want one or two skills out of a large repo, not the whole thing.
- The upstream repo is not APM-aware (and you do not want to ask the
  maintainer to refactor).
- You are pinning a specific primitive at a specific commit without taking
  on the rest of the repo's surface area.

Both forms produce the same artifact in `apm_modules/` and the same compiled
output. The reference syntax is the only difference.

## Decision guide

| Situation                                                | Use            | Does upstream need `.apm/`? |
|----------------------------------------------------------|----------------|-----------------------------|
| Importing one or two skills from a third-party repo      | Primitive form | No                          |
| Publishing your team's full skill set as a bundle        | Package form   | Yes                         |
| Mixed: a curated bundle plus a few file-level imports    | Package form   | Yes (works for both)        |
| Quick test before adopting someone's skill               | Primitive form | No                          |

The short version: **if you are consuming, primitive form covers most cases
without forcing anyone to refactor. If you are publishing, package form is
the right investment.**

If you started authoring directly in `.github/` and later want to make a
proper package, the migration is mechanical: move the files into `.apm/`,
add an `apm.yml`, and run `apm install` to re-generate `.github/` from the
new source. No data loss, no breaking change for downstream consumers.

## Why does microsoft/apm itself have a `.apm/` folder?

Because we use APM to manage the agent context that develops APM. The
[concrete example above](#a-concrete-example-this-repo) is this repo. If
you are looking for a working reference layout, it is right there.

## What APM looks for

Discovery rules, in order:

1. **`apm.yml`** at the repo root marks the directory as an APM package and
   declares its dependencies, scripts, and metadata.
2. **`.apm/`** at the repo root is the source root for primitives. APM does
   not look elsewhere for sources.
3. Inside `.apm/`, primitives are grouped by type subdirectory:

   ```
   .apm/
   +-- skills/         (SKILL.md plus supporting files)
   +-- agents/         (agent definitions)
   +-- instructions/   (instruction files)
   +-- prompts/        (prompt templates)
   +-- chatmodes/      (chat mode configurations)
   +-- context/        (shared context fragments)
   ```

4. **Per-primitive references** (`owner/repo/path/to/primitive`) bypass
   `.apm/` entirely. APM treats the named subdirectory as a single-primitive
   virtual package regardless of where it sits in the upstream repo.
5. **Compiled output** (`.github/`, `.claude/`, `.cursor/rules/`, and other
   runtime targets) is generated by `apm compile` based on the runtimes
   declared in `apm.yml`. Never edit these directly in an APM-managed repo.

For the full schema, see [Manifest Schema](../../reference/manifest-schema/)
and [Primitive Types](../../reference/primitive-types/).

## Quick FAQ

**I edited `.github/skills/my-skill/SKILL.md` directly. What happens on the
next `apm install`?** Your edit gets overwritten. Edit the source under
`.apm/skills/my-skill/SKILL.md` instead and re-run `apm install`.

**I ran `ls` and don't see `.apm/`.** It's a dotfile directory, hidden by
default. Use `ls -a`.

**Do I need `.apm/` to install packages?** No. `.apm/` is for authoring. If
you only consume packages, `apm install` creates the runtime targets
(`.github/`, `.claude/`, etc.) directly under `apm_modules/` and you never
touch `.apm/`.

**What's the minimum for a valid APM package?** `apm.yml` at the root plus
at least one primitive under `.apm/`.

**Isn't the industry converging on the plugin format? Why do I need
`.apm/` at all?** APM consumes plugins natively (`plugin.json` packages
install as first-class dependencies) and exports to plugin format
(`apm pack --format plugin`). `.apm/` is the source layout that gives
you dependency management, lockfiles, and security scanning during
authoring; `plugin.json` is the runtime distribution format. Use both --
see [Why not just ship a `plugin.json`?](#why-not-just-ship-a-pluginjson)
above and the [hybrid authoring workflow](../../guides/plugins/#hybrid-authoring-workflow).

## See also

- [Your First Package](../../getting-started/first-package/) -- create a
  package from scratch using this layout.
- [Primitive Types](../../reference/primitive-types/) -- the canonical
  reference for skills, agents, instructions, prompts, and friends.
- [Manifest Schema](../../reference/manifest-schema/) -- the full `apm.yml`
  spec.
- [gh-aw Integration](../../integrations/gh-aw/) -- how compiled output
  feeds GitHub Agentic Workflows.
- [Compilation](../../guides/compilation/) -- how `.apm/` becomes
  `.github/`, `.claude/`, and the rest.
