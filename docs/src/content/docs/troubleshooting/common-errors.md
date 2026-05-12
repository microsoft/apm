---
title: Common Errors
description: Catalog of the most-hit APM errors with one-line cause and fix per entry.
sidebar:
  order: 1
---

Found a confusing error? Search this page first. Each entry shows the error string APM prints, the underlying cause, and the shortest fix. Deeper guides are linked under "See also".

Errors are grouped by the surface that produces them: install, compile, audit, run, and auth/network.

## Install

### `apm.yml not found. Run 'apm init' first.`

```
apm.yml not found. Run 'apm init' first.
```

Cause: APM was invoked in a directory with no manifest, or the manifest has a different name.

Fix: run `apm init` to scaffold a manifest, or `cd` into the project that owns the `apm.yml`.

See also: [../reference/cli/init/](../reference/cli/init/)

### `lockfile not found at <path>; run 'apm install' to generate it`

```
lockfile not found at apm.lock.yaml; run 'apm install' to generate it
```

Cause: a command that needs a resolved dependency graph (drift check, audit, compile against lockfile) ran before the project had ever been installed.

Fix: run `apm install` once to produce `apm.lock.yaml`, then re-run the original command.

See also: [./install-failures/](./install-failures/), [../reference/lockfile-spec/](../reference/lockfile-spec/)

### `<pkg>: not found in lockfile`

(message similar to)

```
<owner>/<repo>: not found in lockfile
1 ref mismatch(es) -- run 'apm install' to update lockfile
```

Cause: a dependency exists in `apm.yml` but has no entry in `apm.lock.yaml`. Most often hit in CI when the lockfile was not regenerated after editing the manifest.

Fix: run `apm install` locally to refresh `apm.lock.yaml` and commit the result. Do not hand-edit the lockfile.

See also: [./install-failures/](./install-failures/), [../reference/lockfile-spec/](../reference/lockfile-spec/)

### `<pkg>: manifest ref '<ref>' != lockfile ref '<ref>'`

```
<owner>/<repo>: manifest ref 'v2' != lockfile ref 'v1'
N ref mismatch(es) -- run 'apm install' to update lockfile
```

Cause: the version or ref pinned in `apm.yml` was changed without re-running install. The lockfile is stale.

Fix: run `apm install` and commit the updated `apm.lock.yaml`.

See also: [./install-failures/](./install-failures/)

### `Drift detected: N file(s)`

```
[!] Drift detected: N file(s)
[i] Run 'apm install' to re-sync deployed files with the lockfile.
```

Cause: deployed primitive files in the project differ from the content recorded in `apm.lock.yaml`. Either the files were edited locally, or the lockfile was updated without re-running install.

Fix: run `apm install` to re-deploy from the cache. If the local edits were intentional, vendor them into the source package or a local override before re-installing.

See also: [./install-failures/](./install-failures/)

### `Content hash mismatch for <pkg>: expected <a>, got <b>`

```
[x] Content hash mismatch for <owner>/<repo>: expected <sha>, got <sha>.
The downloaded content differs from the lockfile record. This may
indicate a supply-chain attack. Use 'apm install --update' to accept
new content and update the lockfile.
```

Cause: the bytes APM downloaded for a locked dependency do not match the `content_hash` recorded in `apm.lock.yaml`. Usually a force-pushed tag, a mutated branch, or a tampered mirror.

Fix: investigate the upstream package before accepting. If the change is legitimate, run `apm install --update` to re-pin and commit the new hash.

See also: [./install-failures/](./install-failures/), [../reference/lockfile-spec/](../reference/lockfile-spec/)

### `Install blocked by org policy -- see violations above`

```
[x] Install blocked by org policy -- see violations above
```

Cause: an org-level policy (compilation target, dependency allowlist, security baseline) rejected one or more dependencies. The per-violation lines printed above this message identify the offending packages and rules.

Fix: read the violations above the summary line, remove or replace the offending packages, or update org policy if the rule is wrong.

See also: [./policy-debugging/](./policy-debugging/)

### `from unapproved host(s): <host>`

(message similar to)

```
[x] Refusing to install from unapproved host(s): <host>.
```

Cause: a dependency resolves to a host outside the configured allowlist.

Fix: add the host to the allowlist, or pass `--allow-insecure-host <host>` for a one-off install. Do not bypass the allowlist in CI.

See also: [./policy-debugging/](./policy-debugging/)

### `Race condition: concurrent checkout failed integrity for <url> @ <sha>`

```
Race condition: concurrent checkout failed integrity for <url> @ <sha>
```

Cause: two `apm install` processes raced on the same cache shard and the loser's integrity check failed. The cache shard lock is per-shard with a 120s default timeout.

Fix: re-run `apm install`. If it reproduces, ensure only one install runs at a time per `APM_CACHE_DIR`. In CI, give each job its own cache directory.

See also: [./install-failures/](./install-failures/)

## Compile

### `No primitives matched`

(message similar to) printed when compile produces zero output.

Cause: no primitives in the project (or filtered subset) match the active target's compilation rules. Common causes: wrong `--target`, missing frontmatter, glob filter matched nothing, or org policy stripped everything.

Fix: run `apm compile --dry-run --verbose` to see what was discovered and why each primitive was skipped.

See also: [./compile-zero-output-warning/](./compile-zero-output-warning/), [../reference/cli/compile/](../reference/cli/compile/)

### `[x] No harness detected`

```
[x] No harness detected
```

Cause: the project has no harness markers (`.github/copilot/`, `.claude/`, `.cursor/`, ...) and no `targets:` block in `apm.yml`. APM no longer defaults to copilot.

Fix: pass `--target <harness>` on the command, or declare `targets:` in `apm.yml`. Run `apm targets` to list supported harnesses.

See also: [../reference/cli/targets/](../reference/cli/targets/), [../reference/targets-matrix/](../reference/targets-matrix/)

### `[x] Multiple harnesses detected: <a>, <b>`

Cause: APM found markers for more than one harness and cannot pick.

Fix: pin a target with `--target <harness>` or declare `targets:` in `apm.yml`. Use `apm install --dry-run` to preview each option.

See also: [../reference/cli/targets/](../reference/cli/targets/)

### `[x] Unknown target '<value>'`

```
[x] Unknown target '<value>'
Valid targets: claude, copilot, cursor, ...
```

Cause: the value passed to `--target` (or listed in `apm.yml` `targets:`) is not a known harness.

Fix: run `apm targets` for the canonical list. Check for typos.

See also: [../reference/cli/targets/](../reference/cli/targets/), [../reference/targets-matrix/](../reference/targets-matrix/)

## Audit

### `Policy violation: <dep> -- <reason>`

```
[x] Policy violation: <owner>/<repo>@<ref> -- <reason>
```

Cause: a dependency failed an org policy check during install or audit. The reason field names the rule.

Fix: see [./policy-debugging/](./policy-debugging/) for resolving each rule type. For audit-only reporting, use `apm audit --format sarif` or `--format json` to feed the findings into your code-scanning pipeline.

See also: [./policy-debugging/](./policy-debugging/), [../reference/cli/audit/](../reference/cli/audit/)

### `Policy hash mismatch from <source>: expected <a>, got <b>`

Cause: the org policy file fetched from the configured source does not match the pinned hash. Either the policy was updated upstream and the pin is stale, or the source was tampered with.

Fix: confirm the upstream change is legitimate, then update the policy pin in your APM config to the new hash.

See also: [./policy-debugging/](./policy-debugging/)

## Run

### `Script or prompt '<name>' not found.`

```
Script or prompt '<name>' not found.
Available scripts in apm.yml: <list>
```

Cause: `apm run <name>` could not match `<name>` to a script in `apm.yml`, an auto-discovered prompt file, or an installable virtual package.

Fix: check the available scripts list printed under the error. To list discoverable prompts, run `apm list`. To install a remote prompt, use `apm install <owner>/<repo>/path/to/prompt.prompt.md`.

See also: [../reference/cli/run/](../reference/cli/run/), [../reference/cli/list/](../reference/cli/list/)

### `No such command '<x>'`

```
Error: No such command '<x>'.
```

Cause: standard Click error. The subcommand does not exist, or you are on an older APM version that lacks it.

Fix: run `apm --help` for the current command list. Upgrade APM if the command was added in a newer release.

See also: [../reference/cli/](../reference/cli/), [./migration/](./migration/)

## Auth and network

### `Authentication failed for <host>`

```
[x] Authentication failed for <host>
... -- check `gh auth status` and `GITHUB_APM_PAT`
```

Cause: the remote host rejected APM's credentials, or no credentials were available. Token resolution order for GitHub: per-org `GITHUB_APM_PAT_<ORG>`, then `GITHUB_APM_PAT`, then `GITHUB_TOKEN`, then `GH_TOKEN`, then `gh auth token`, then git credential helpers.

Fix: set `GITHUB_APM_PAT` (or run `gh auth login`) with a token that has access to the dependency's repo. For Azure DevOps, set `ADO_APM_PAT`. The diagnostic block printed under the error names the host and the resolution attempts that failed.

See also: [./install-failures/](./install-failures/)

### `TLS verification failed`

```
TLS verification failed -- if you're behind a corporate proxy or
firewall, set the REQUESTS_CA_BUNDLE environment variable to the
path of your organisation's CA bundle (a PEM file) and retry.
```

Cause: Python's TLS stack rejected the server certificate. Almost always a corporate proxy doing TLS interception with a CA that is not in the system trust store.

Fix: export `REQUESTS_CA_BUNDLE=/path/to/corporate-ca.pem` and retry. Do not disable TLS verification.

See also: [./ssl-issues/](./ssl-issues/)

### Network timeout or host unreachable

(no fixed string; surfaces as `requests` / git transport errors after the 15s API or 30s git timeout)

Cause: the host is unreachable, the proxy is down, or DNS is failing.

Fix: confirm `curl -v https://<host>` works from the same shell. Check `HTTPS_PROXY` / `NO_PROXY` are set correctly. Re-run `apm install --verbose` to see which host failed and on which leg (API probe vs git fetch).

See also: [./ssl-issues/](./ssl-issues/), [./install-failures/](./install-failures/)
