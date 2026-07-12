---
title: apm deny
description: Block executable primitives from dependency packages.
sidebar:
  order: 26
---

## Synopsis

```bash
apm deny PACKAGE_REF... [--user]
```

## Description

`apm deny` blocks executable primitives from one or more dependency packages.
By default, it writes the decision to the project's committed
`apm.yml` under `executables.deny`. Pass `--user` to write a machine-local
decision to `~/.apm/config.json` instead.

A deny takes precedence over an allow. For the full precedence model and the
executable types covered by the gate, see [`apm approve`](../approve/).

## Arguments and options

| Argument or flag | Description |
|---|---|
| `PACKAGE_REF...` | One or more package references, such as `owner/repo`. Required. |
| `--user` | Record the deny in `~/.apm/config.json` instead of `apm.yml`. |

For an installed package, APM records the executable types that package
declares. A package that is not installed can still be denied pre-emptively;
APM blocks all supported executable types for that reference. Writing a deny
also removes a matching allow from the selected store.

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

- [`apm approve`](../approve/) -- manage executable trust and inspect the
  deny-wins precedence model.
- [`apm policy`](../policy/) -- explain the effective trust decision for an
  installed package.
- [`apm install`](../install/) -- install dependencies while enforcing the
  executable gate.
