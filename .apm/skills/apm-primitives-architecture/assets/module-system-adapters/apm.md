# Module-System Adapter: APM

Loaded at design step 7b (coder phase), and ONLY when the handoff
packet from step 6 lists at least one EXTERNAL MODULE under
"external modules required".

This adapter does not re-document APM. It maps the durable
substrate concepts (from `../composition-substrate.md`) to the
canonical APM usage knowledge that already exists, then delegates.

## Mapping

| Substrate concept     | APM realization (delegate to `apm-usage`)            |
|-----------------------|------------------------------------------------------|
| MODULE                | APM package (or plugin / claude skill / hook package) |
| DEPENDENCY            | entry under `dependencies.apm` in the manifest        |
| DISTRIBUTION BOUNDARY | published git ref (or local path for dev)             |
| TRANSITIVE CLOSURE    | resolved + locked dependency tree                     |
| VERSION PINNING       | ref pin in the dep spec; lockfile records resolution  |
| PORTABILITY MODE      | hybrid authoring (manifest + plugin export)           |

## Delegation

Load the `apm-usage` skill at this point. It is the source of
truth for:

- manifest filename, schema, dependency spec syntax
- supported dependency types and detection rules
- CLI surface (`apm install`, `apm pack`, `apm compile`, ...)
- lockfile format and conflict resolution
- plugin / hybrid authoring and export

`apm-usage` lives in `packages/apm-guide/.apm/skills/apm-usage/`
within this repository, and is also published as the standalone
`apm-guide` package for consumers who want APM usage knowledge in
their own projects.

## Coder rules

1. Use ONLY the substrate concept vocabulary in the module body
   itself. APM-specific syntax (manifest snippets, CLI commands)
   appears only in onboarding/installation sections of a module,
   never in its design discussion or interface sketch.

2. When a module declares EXTERNAL MODULE dependencies, emit the
   manifest entries by consulting `apm-usage`; never invent the
   syntax.

3. If a future module-system tool replaces APM, add a sibling
   adapter (`module-system-adapters/<new-tool>.md`) and switch the
   project default. The substrate file and architect persona stay
   unchanged. (Open-closed.)

## Why a thin pointer, not a copy

The substrate captures durable concepts; `apm-usage` captures
today's tool. Duplicating APM syntax here would create exactly the
DUPLICATED LEAF anti-pattern the substrate file warns about. This
adapter is the canonical example of "depend, don't duplicate".
