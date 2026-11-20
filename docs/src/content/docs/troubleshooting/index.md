---
title: Troubleshoot
description: Find your symptom, run two or three commands to see the cause, and follow the link to the fix.
---

Something did not work and you want the fix fast. Find the symptom closest to yours, run the
commands under it, and follow the link. Looking up an exact error message instead? Go to
[Common errors](./common-errors/) or [Install failures](./install-failures/).

Start with `apm doctor` for most problems: it checks git, network access and credentials,
and exits non-zero when a critical check fails.

## Nothing was installed (no harness detected)

**What you see:** `Error: No harness detected`, and `apm install` exits with code 2.

APM writes files for harnesses, the agent tools that read them (`.github/` for GitHub
Copilot, `.claude/` for Claude Code). Your `apm.yml` names none in `targets:`, and the
repository has no harness folder for APM to detect. `apm targets` shows what APM found.

```bash frame="terminal"
apm targets
apm install <package> --target copilot,claude
```

When this command creates `apm.yml`, it records the harnesses under `targets:`; in an existing
`apm.yml`, add `targets:` yourself. See
[Choose your harnesses](../consumer/choose-harnesses/).

## A plugin installed but is missing in Claude Code

**What you see:** `apm install` reported the plugin, but `/plugin` in Claude Code does not list
it.

APM unpacks a Claude plugin into project files: commands to `.claude/commands/`, skills to
`.claude/skills/`, agents to `.claude/agents/`. Claude Code loads them as project files, not as
an installed plugin. Check that the package is installed, that `claude` is an active target,
and where a file came from:

```bash frame="terminal"
apm deps list
apm targets
apm find .claude/commands/commit.md
```

If `claude` shows `inactive`, nothing was written to `.claude/`. See
[Install plugins](../consumer/install-plugins/).

:::note[Planned]
Installing a plugin as one locked, approved plugin root that each harness registers natively is
tracked in [#3094](https://github.com/microsoft/apm/issues/3094).
:::

## 401 or 403 from GitHub Enterprise, Azure DevOps or GitLab

**What you see:** `Authentication failed for <operation> on <host>`.

The host rejected APM's credentials, or APM found none. Each host reads its own variable:
`GITHUB_APM_PAT` for GitHub and GitHub Enterprise, `ADO_APM_PAT` for Azure DevOps,
`GITLAB_APM_PAT` for GitLab. `apm auth` validates a GitHub, GitHub Enterprise or GitLab token
against the host:

```bash frame="terminal"
apm doctor
apm auth github.example.com --check
```

A 403 with a valid token usually means the token lacks access to that repository or is not
authorized for the organization's SSO. See [Use private sources](../getting-started/authentication/).

## TLS or proxy errors

**What you see:** `TLS verification failed`, `CERTIFICATE_VERIFY_FAILED`, or
`unable to get local issuer certificate`.

A corporate proxy or an internal server presents a certificate issued by a CA your machine does
not trust. Add the CA without dropping public trust, and set the standard proxy variables if
you use one:

```bash frame="terminal"
export APM_EXTRA_CA_BUNDLE=/path/to/corporate-ca.pem
export HTTPS_PROXY=http://proxy.example.com:8080
apm install --verbose
```

See [SSL / TLS issues](./ssl-issues/).

## `apm install --frozen` fails in CI

**What you see:** `apm install --frozen` exits 1 in CI but `apm install` works on your laptop.

`--frozen` installs exactly what `apm.lock.yaml` records and never rewrites it. It fails when
the lockfile is missing, when `apm.yml` changed without a lockfile update, or when a deployed
file differs from its locked hash; in that case nothing is written. For edited files, keep
the change with `apm export-patch`, then delete the files and rerun `apm install`, or run
`apm install --force`. Otherwise, reproduce it locally, then fix and commit:

```bash frame="terminal"
apm install --frozen
apm install
git add apm.yml apm.lock.yaml
```

Commit the deployed files too if your repository tracks them. CI also needs credentials for
private sources. See [Run in CI](../consumer/run-in-ci/).

## Policy blocked my install

**What you see:** `Install blocked by org policy -- see violations above`.

Your organization's `apm-policy.yml` does not allow a source or package you added. The lines
above the error name the rule, for example `dependency-allowlist`. Check which policy applied
and whether it blocks or warns:

```bash frame="terminal"
apm policy status
```

Use an allowed source, or ask the policy owner for an exception. `--no-policy` skips the check
for one local run, but `apm audit --ci` still enforces it. See
[Set an org policy](../enterprise/apm-policy/).

## A file changed and audit reports drift

**What you see:** `[x] Drift detected: 1 file(s)`, with the path under `modified`.

A deployed file no longer matches the hash in the lockfile, usually because someone edited it.
APM keeps local edits on install, so the drift stays until you choose. Find the package that
owns the file:

```bash frame="terminal"
apm audit
apm find --source .claude/skills/style-checker/SKILL.md
```

To keep the change, `apm export-patch` writes it as a patch for the package's author. To discard
it, run `apm install --force` (plain `apm install` keeps your edit). See [Inventory and drift](../enterprise/drift-detection/).

## `apm list` does not show my packages

**What you see:** `[!] No scripts found.`

`apm list` lists scripts from `apm.yml`. Packages are under `apm deps`:

```bash frame="terminal"
apm deps list
apm deps tree
apm deps why <package>
```

See [Add and remove packages](../consumer/install-packages/).

## Still stuck

Collect this, then open an issue in the [APM issue tracker](https://github.com/microsoft/apm/issues)
with your `apm.yml` and `apm.lock.yaml`:

```bash frame="terminal"
apm --version
APM_DEBUG=1 apm <failing-command> --verbose 2>&1 | tee apm-debug.log
apm cache info
```
