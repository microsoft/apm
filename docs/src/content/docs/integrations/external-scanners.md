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

## Configure scanner behaviour

By default SkillSpector runs **offline and deterministic** (APM passes
`--no-llm`). SkillSpector can also run an **LLM-powered** analysis that produces
richer findings, but it needs an API key and makes outbound network calls. You
opt into it explicitly.

### LLM mode

```bash
# One run: force LLM analysis on (overrides config)
apm audit --external skillspector --external-llm

# One run: force it off
apm audit --external skillspector --no-external-llm
```

LLM mode requires an API key in your environment (`OPENAI_API_KEY` or
`NVIDIA_INFERENCE_KEY`). If `--external-llm` is set and no key is present, the
scan **fails closed** with an actionable message -- APM never falls back to a
silent offline run. When LLM mode is active APM prints a one-line egress banner
before the scan:

```
[!] LLM analysis enabled for 'skillspector' -- outbound API calls will be made (network egress; API billing may apply)
```

The API keys are read from your own environment only when LLM mode is active;
APM never stores them and strips them from the scanner subprocess otherwise.

### Extra arguments (allowlisted)

Pass extra CLI flags to the scanner with `--external-args` (a single
shlex-split string):

```bash
apm audit --external skillspector --external-args "--model gpt-4o --severity high"
```

For safety, **only an allowlist of safe flag prefixes** is accepted (for
SkillSpector: `--model`, `--severity`, `--threshold`, `--profile`, `--lang`,
`--exclude`, `--include`, and similar). Any token that is not allowlisted, that
looks like a secret (`--token`, `--api-key`, ...), or that points to a path
outside the working directory is **rejected fail-closed** -- the scan does not
run. `--external-args` and `--external-llm` both require `--external <name>`;
used alone they raise a usage error.

:::caution[Policy floor is install-only]
`allow_args` restrictions in `apm-policy.yml` apply during `apm install`. A bare
`apm audit` run does **not** load org policy, so extra-args safety relies solely
on the adapter allowlist described above -- not the policy floor. To enforce a
scanner kill-switch over ad-hoc developer audits, gate it in CI (see
[Run an audit during `apm install`](#run-an-audit-during-apm-install)).
:::

### Persisted config

Set personal defaults so you do not repeat the flags (both keys are gated on the
`external-scanners` flag):

```bash
apm config set external.skillspector.llm true
apm config set external.skillspector.args "--model gpt-4o"
apm config get external.skillspector.llm
apm config unset external.skillspector.args
```

CLI flags override config for that run. The JSON is stored owner-only under
`external_scanners.<name>` in `~/.apm/config.json`.

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
    scanners:                  # optional: per-scanner governance
      skillspector:
        allow_args: false      # forbid extra-args passthrough (kill-switch)
```

The optional `scanners` block lets an org **restrict** scanner behaviour. It is
**restrict-only**: `allow_args: false` strips any user/CLI extra-args for that
scanner at install time, locking it to a vetted invocation. Policy never *adds*
argv tokens and never forces LLM mode on -- it can only tighten. `allow_args` is
AND-merged across an inheritance chain (any ancestor setting `false` wins). See
the [policy schema reference](../../reference/policy-schema/#per-scanner-governance-auditscanners)
for the full schema.

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
- **Policy floor is an install-time control.** The `scanners.<name>.allow_args`
  kill-switch is enforced during `apm install` (where org policy is loaded). A
  bare `apm audit` run does not load org policy, so it relies on the adapter's
  allowlist validation for arg safety rather than the policy floor.

See the [`apm audit` reference](../../reference/cli/audit/) for the full option
list.
