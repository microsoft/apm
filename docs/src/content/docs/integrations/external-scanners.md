---
title: "External scanners"
description: "Ingest SARIF from third-party skill/security scanners into apm audit (experimental)."
sidebar:
  order: 7
  badge:
    text: Experimental
    variant: caution
---

:::caution[Experimental]
This feature is behind the `external-scanners` experimental flag and is
off by default. The CLI surface may change. Enable it explicitly before use.
:::

`apm audit` ships with its own content scanner for hidden-Unicode attacks. You
can additionally fold in findings from **any SARIF 2.1.0 scanner** -- for
example NVIDIA SkillSpector or a generic tool such as Semgrep or CodeQL -- so
a single `apm audit` run reports APM's native findings *and* the external
tool's findings through the same text / JSON / SARIF / markdown output and
exit codes.

This is a **one-directional** integration: APM only *reads* the SARIF the
external tool produces. APM publishes nothing back, and this is not a vendor
partnership -- any SARIF-emitting tool works.

## Enable the feature

```bash
apm experimental enable external-scanners
```

The opt-in is entirely CLI-driven and **install-method-neutral**: it works the
same whether you run APM from source or as the self-contained binary. There is
no extra Python package to `pip install`.

## Ingest a SARIF file (works with the APM binary)

The simplest, most portable path: have any scanner emit a SARIF file, then
hand it to `apm audit`.

```bash
# 1. Produce SARIF with the tool of your choice
semgrep --sarif --output report.sarif .

# 2. Fold its findings into apm audit
apm audit --external sarif --external-sarif report.sarif
```

External findings merge into APM's report and drive the exit code using the
same severity scale (SARIF `error` -> critical -> exit **1**, `warning` ->
exit **2**, `note` -> info, non-gating).

## Invoke a scanner CLI on PATH

When a scanner exposes a CLI that emits SARIF, APM can invoke it directly.
SkillSpector is supported by name -- APM runs it when the `skillspector`
executable is resolvable on your `PATH`:

```bash
apm audit --external skillspector
```

If the CLI is not on `PATH`, APM tells you so and points you back to the
file-based path above (`--external sarif --external-sarif <file>`), which needs
no installation.

## Run an audit during `apm install`

The same machinery can run **during install**, scanning the files a package
just deployed before you start trusting them. This is off by default; the
`external-scanners` flag must be enabled, and then you choose a mode:

```bash
# One-off: warn (record findings) or block (halt on critical findings)
apm install some/package --audit warn
apm install some/package --audit block

# Disable for a single invocation
apm install some/package --no-audit
```

Set a personal default so every install audits without a flag:

```bash
apm config set audit-on-install warn   # off | warn | block
```

Organizations can mandate it through `apm-policy.yml`:

```yaml
security:
  audit:
    on_install: block          # off | warn | block
    external:                  # optional: scanners that MUST run at install
      - skillspector
```

Policy is a **floor**: it can raise the effective mode but a weaker
`--audit`/config value can never relax an org `block`. `apm install
--no-policy` skips the floor for that invocation. `--audit block` (or
`--force`) always lets you tighten or override locally.

When policy lists required `external` scanners, they run as part of the
install audit. If a required scanner is **not available** at install time
(for example its CLI is not on `PATH`), the install **fails closed** with a
clear, actionable message rather than silently skipping the check.

## Notes

- **Additive, never weakening.** APM's native content scan always runs. External
  scanners only *add* findings; they never replace or relax APM's own checks.
- **Repeatable.** Pass `--external` more than once to combine scanners.
- **Not in `--ci` yet.** Run external scanners in bare `apm audit` mode.
- **Fail-closed.** Without the experimental flag, `--external` exits non-zero
  with an actionable message.

See the [`apm audit` reference](../../reference/cli/audit/) for the full option
list.
