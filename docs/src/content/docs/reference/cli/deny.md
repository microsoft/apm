---
title: apm deny
description: Block executable primitives from dependency packages.
sidebar:
  order: 26
---

## Synopsis

```bash
apm deny [OPTIONS] PACKAGES...
```

## Description

`apm deny` blocks executable primitives from one or more dependency packages.
By default, it writes the decision to the project's committed
`apm.yml` under `executables.deny`. Pass `--user` to write a machine-local
decision to `$APM_HOME/config.json` (default `~/.apm/config.json`) instead. Without `--user`, run
the command from an APM project. `--user` also works outside a project and
reads the packages `apm install -g` installed.

A deny takes precedence over an allow. For the full precedence model and the
executable types covered by the gate, see
[`apm approve` -- Precedence](../approve/#precedence-deny-wins-first-match-wins).

## Arguments and options

| Argument or flag | Description |
|---|---|
| `PACKAGES...` | One or more package references, such as `owner/repo`. Required. |
| `--user` | Record the deny in `$APM_HOME/config.json` (default `~/.apm/config.json`) instead of `apm.yml`. |

For an installed package that declares executable types, APM records those
types. When no executable declaration is found -- because the package is not
installed or declares no executables -- APM blocks all supported executable
types for that reference. Writing a deny also removes a matching allow from the
selected store. To lift a deny, run `apm approve <pkg>` against the same store
(add `--user` for a `--user` deny).

The next `apm install` or `apm update` stops deploying the denied package's
hooks and removes the hook entries and scripts it deployed earlier from every
harness config. Removing an approval while the gate is on has the same effect.
Until then, `apm audit --ci` reports the leftover hooks as drift.
`apm install --frozen` also removes them, but cannot update `apm.lock.yaml`,
so it exits `1` naming each retired file and the decision that retired it.
Run `apm install` without `--frozen` and commit `apm.lock.yaml`.
`apm install --dry-run` previews the retirement without removing anything.

## Examples

Block a package for everyone using the project:

```bash
apm deny owner/repo
```

Block multiple packages:

```bash
apm deny owner/first owner/second
```

Block a package only on the current machine:

```bash
apm deny --user owner/repo
```

## Related

- [`apm approve` -- Precedence](../approve/#precedence-deny-wins-first-match-wins)
  -- manage executable trust and inspect the deny-wins precedence model.
- [`apm policy`](../policy/) -- explain the effective trust decision for an
  installed package.
- [`apm install`](../install/) -- install dependencies while enforcing the
  executable gate.
