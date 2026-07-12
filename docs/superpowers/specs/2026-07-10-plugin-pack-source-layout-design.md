# Plugin Pack Source Layout Design

Date: 2026-07-10
Status: Approved
Related: issue #2054, PR #2122

## Decision

`apm pack` uses the presence of the project-root `.apm/` directory as
the source-layout switch:

- When `.apm/` exists, `.apm/` is the authoritative local primitive
  source. Root convention directories are not packed implicitly.
- When `.apm/` does not exist, supported root convention directories
  remain authoritative and are packed implicitly.
- An explicit `includes` list is exhaustive regardless of layout.
- `includes: auto` grants publication consent but does not select the
  source layout.

When both `.apm/` and supported root convention directories exist,
`.apm/` wins. Packing succeeds and emits an actionable warning for
each skipped root directory.

## Problem

Issue #2054 demonstrates a publication-boundary bug: an APM-authored
repository can contain an unrelated root `skills/` working directory,
and `apm pack` currently includes it in a plugin bundle.

PR #2122 fixes that symptom by skipping root convention directories
whenever `includes` is declared. That discriminator is incorrect
because `includes` represents publication consent, not source layout.

Official Claude plugin documentation defines `skills/`, `agents/`,
`commands/`, and `hooks/` at the plugin root as native authoring
locations:

https://code.claude.com/docs/en/plugins

A Claude plugin author can therefore have valid publishable root
content, run `apm init`, receive `includes: auto`, and then silently
lose that content from `apm pack`. APM must support progressive
adoption without making vendor-native content disappear.

## Goals

1. Prevent unrelated root convention directories from leaking into
   bundles after a project adopts `.apm/`.
2. Preserve native Claude plugin behavior before `.apm/` adoption.
3. Keep `includes` semantics independent from source-layout detection.
4. Make mixed-layout behavior deterministic and explainable.
5. Support gradual migration from plugin-native layout to `.apm/`.

## Non-goals

- Merge `.apm/` and root convention trees implicitly.
- Add a `source_layout` field to `apm.yml`.
- Change dependency package discovery.
- Move files automatically during `apm init` or `apm pack`.
- Change README claims without explicit maintainer approval.

## Behavioral Contract

| Includes | `.apm/` | Root convention dirs | Implicit root packing |
|----------|---------|----------------------|-----------------------|
| omitted | absent | present | yes |
| `auto` | absent | present | yes |
| omitted | present | absent | no |
| `auto` | present | absent | no |
| omitted | present | present | no, with warning |
| `auto` | present | present | no, with warning |
| explicit list | either | either | no; only listed paths |

Supported root convention directories remain the set already recognized
by plugin packing. This decision changes when that existing discovery
logic runs; it does not expand the recognized set.

## Precedence

Packing resolves local content in this order:

1. If `includes` is an explicit path list, pack only those paths.
2. Otherwise, detect whether the project-root `.apm/` directory exists.
3. If `.apm/` exists, collect local primitives from `.apm/` and skip
   implicit root convention discovery.
4. If `.apm/` does not exist, collect local primitives from supported
   root convention directories.
5. Continue using existing dependency component discovery independently
   of the root project's source-layout decision.

Directory presence is intentional. An empty `.apm/` directory indicates
that the author has switched to APM-native layout. This prevents
half-migrated root content from being merged silently.

## Mixed-layout UX

When `.apm/` and one or more supported root convention directories both
exist, packing succeeds and writes one warning per skipped directory to
stderr:

```text
[!] Skipping root-level skills/ because .apm/ is present.
    Move publishable files to .apm/skills/ or remove skills/ to silence
    this warning.
```

The warning names:

1. the skipped directory,
2. the cause,
3. the next action.

A mixed layout is not an error because migration can occur incrementally.
Implicitly merging both trees is rejected because collision provenance
would become difficult to predict and debug.

If packing finds no local primitives after applying the source-layout
rule, it emits:

```text
[!] No local primitives found. Expected content under .apm/.
    Check the project layout or move plugin-native content into .apm/.
```

## `apm init` Onboarding

`apm init` must not make an existing native Claude plugin stop packing.

When root convention directories exist and `.apm/` does not:

1. `apm init` may continue writing `includes: auto`.
2. It does not create `.apm/` implicitly.
3. It reports that native root directories remain pack sources.
4. It explains how to opt into `.apm/` by moving content.

Suggested output:

```text
[!] Found plugin-native directories at the project root: skills/.
    They remain included by apm pack. Move them to .apm/skills/ when
    adopting the APM source layout.
```

## Error Handling

An explicit `includes` path that does not exist is a configuration
error, not a layout warning:

```text
[x] includes path '.apm/skills/example' does not exist.
    Fix the path in apm.yml or create it.
```

The error names the failure, cause, and next action. No implicit fallback
to root convention discovery occurs for an invalid explicit list.

## Compatibility

This design preserves:

- plugin-native repositories with omitted `includes`,
- plugin-native repositories after `apm init` adds `includes: auto`,
- APM-native repositories that author under `.apm/`,
- explicit allow-list behavior,
- dependency package convention discovery.

The intentional behavior change is for mixed repositories: once `.apm/`
exists, root convention directories stop being implicit pack sources and
produce warnings. Authors can still include a root path through an
explicit list when that is a deliberate publication decision.

## Acceptance Tests

1. `includes: auto`, `.apm/skills/published/`, and root `skills/wip/`
   packs only the `.apm/` skill.
2. `includes: auto`, no `.apm/`, and root `skills/published/` packs the
   root skill.
3. Omitted `includes`, `.apm/skills/published/`, and root `skills/wip/`
   packs only the `.apm/` skill.
4. Omitted `includes`, no `.apm/`, and root `skills/published/` packs
   the root skill.
5. An explicit list packs only listed paths with or without `.apm/`.
6. `.apm/` plus root `skills/` emits a warning naming `skills/`, the
   `.apm/` cause, and a move-or-remove action.
7. `apm init` in a Claude-native plugin leaves root skills packable.
8. Creating `.apm/` after initialization switches authority and causes
   root skills to be skipped with a warning.
9. An invalid explicit path fails with the path and a corrective action.
10. Dependency package root conventions continue to be discovered.

The e2e regression trap must execute the user-visible `apm init` and
`apm pack` flow for a native Claude plugin. Mutation-break proof removes
the `.apm/` presence guard and confirms that the test fails.

## Documentation Impact

PR #2122 documentation must describe the conditional rule:

> When `.apm/` exists, local primitive content is sourced from `.apm/`.
> Without `.apm/`, supported plugin-native root directories remain pack
> sources.

The affected Starlight pages and APM usage guidance include:

- producer pack guidance,
- repository shape guidance,
- pack CLI reference,
- manifest schema reference,
- first-package onboarding,
- package-authoring agent guidance.

README drift, if any, requires separate explicit maintainer approval.

## PR #2122 Disposition

PR #2122 should be revised before merge. Its publication-safety goal is
correct, but `has_declared_includes` must not select the source layout.
The revised change should use `.apm/` presence, add mixed-layout
warnings, and cover the native-plugin-after-`apm init` journey.
