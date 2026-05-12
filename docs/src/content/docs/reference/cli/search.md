---
title: apm search
description: Search a registered marketplace for plugins by query
sidebar:
  order: 21
---

Find plugins in a registered marketplace by name or keyword.

## Synopsis

```bash
apm search QUERY@MARKETPLACE [OPTIONS]
```

`apm search` is a top-level alias for [`apm marketplace search`](../marketplace/). Both commands invoke the same implementation and accept identical arguments and options.

## Description

`apm search` queries a single marketplace registered in the local marketplace registry and returns plugins whose name or description matches `QUERY`. The `QUERY@MARKETPLACE` expression is required: the marketplace name disambiguates which registered source to search, and prevents accidental wide scans across every marketplace.

The marketplace must be registered first with `apm marketplace add`. To list registered marketplaces, run `apm marketplace list`. To browse every plugin in a marketplace without filtering, use `apm marketplace browse <name>`.

Results print as a table with plugin name, description, and the install expression. Pipe the install expression directly into [`apm install`](../install/) to add a hit to your manifest.

## Arguments

| Argument              | Required | Description                                                          |
| --------------------- | -------- | -------------------------------------------------------------------- |
| `QUERY@MARKETPLACE`   | yes      | Search term and registered marketplace name, joined with `@`         |

The expression is split on the last `@`, so queries containing `@` are preserved on the left side.

## Options

| Option              | Default | Description                          |
| ------------------- | ------- | ------------------------------------ |
| `--limit N`         | `20`    | Maximum number of results to display |
| `-v`, `--verbose`   |         | Show detailed output and tracebacks  |

## Examples

Search the `skills` marketplace for security-related plugins:

```bash
apm search security@skills
```

Limit results to the top five matches:

```bash
apm search auth@skills --limit 5
```

Install a result returned by `search`:

```bash
apm install code-review@skills
```

## Exit codes

| Code | Meaning                                                                 |
| ---- | ----------------------------------------------------------------------- |
| `0`  | Search completed (including zero matches)                               |
| `1`  | Invalid expression, marketplace not registered, or unexpected error     |

## Related

- [`apm marketplace`](../marketplace/) -- full marketplace command group (`add`, `list`, `browse`, `refresh`, `remove`, `search`)
- [`apm install`](../install/) -- install a plugin returned by search
- [Use marketplaces](../../../consumer/use-marketplaces/) -- consumer guide for registering and consuming marketplaces
