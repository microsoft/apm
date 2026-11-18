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
apm marketplace search QUERY@MARKETPLACE [OPTIONS]
```

Both command forms use the same search implementation and accept the
`QUERY@MARKETPLACE` expression and options shown below.

## Description

Search one marketplace registered in the local marketplace registry for
plugins whose name or description matches `QUERY`. The marketplace name
disambiguates the source and prevents scans across every registered marketplace.

The marketplace must be registered first with `apm marketplace add`. To list registered marketplaces, run `apm marketplace list`. To browse every plugin in a marketplace without filtering, use `apm marketplace browse <name>`.

Text output is a table with plugin name, description, and install expression.
Pipe an install expression into [`apm install`](../install/) to add a result
to your manifest.

## Arguments

| Argument              | Required | Description                                                          |
| --------------------- | -------- | -------------------------------------------------------------------- |
| `QUERY@MARKETPLACE`   | yes      | Search term and registered marketplace name, joined with `@`         |

The expression is split on the last `@`, so queries containing `@` are preserved on the left side.

## Options

| Option              | Default | Description                          |
| ------------------- | ------- | ------------------------------------ |
| `--limit N`         | `20`    | Maximum number of results to display |
| `--format`, `-f`    | `text`  | Output format: `text` or `json`      |
| `-v`, `--verbose`   |         | Show detailed output and tracebacks  |

## JSON output

Use `--format json` or `-f json` to emit a version-1 document on stdout.
Text remains the default. Success output has `data` as an array:

```json
{"schema_version":1,"data":[{"name":"code-review","description":"Reviews changes","install":"code-review@skills"}]}
```

Each result contains `name`, `description`, and `install`; a missing
description is `null`. A successful search with no matches returns
`{"schema_version":1,"data":[]}` and exits `0`. The existing `--limit`
selects results first; JSON then sorts only those selected results
case-insensitively by `name`.

URL-bearing values in descriptions have userinfo, query strings, and
fragments removed; embedded APM-internal cache paths are replaced with
`[internal-cache]`.

Progress and diagnostics go to stderr. Handled failures write
`{"schema_version":1,"error":"<code>"}` to stderr and exit nonzero. The
stable error codes are `invalid_search_expression`, `marketplace_not_found`,
and `marketplace_search_failed`; raw exception text is not included in the
code. Click parser and option errors retain their normal usage-error output.

## Examples

Search the `skills` marketplace for security-related plugins:

```bash
apm search security@skills
```

The marketplace-group spelling is an alias for the same search:

```bash
apm marketplace search security@skills
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
| `1`  | Handled search failure, including an invalid expression or unregistered marketplace |
| `2`  | Click usage or option error                                               |

## Related

- [`apm marketplace`](../marketplace/) -- full marketplace command group (`add`, `list`, `browse`, `update`, `remove`)
- [`apm install`](../install/) -- install a plugin returned by search
- [Install from marketplaces](../../../consumer/installing-from-marketplaces/) -- consumer guide for registering and consuming marketplaces
