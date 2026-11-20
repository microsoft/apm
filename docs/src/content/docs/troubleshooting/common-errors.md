---
title: Common Errors
description: Catalog of the most-hit APM errors with one-line cause and fix per entry.
sidebar:
  order: 1
---

Each entry shows the error string APM prints, its cause, and the shortest fix, grouped by surface: install, compile, audit, run, and auth/network.

## Install

### `apm.yml not found. Run 'apm init' first.`

```
apm.yml not found. Run 'apm init' first.
```

Cause: APM was invoked in a directory with no manifest, or the manifest has a different name.

Fix: run `apm init` to scaffold a manifest, or `cd` into the project that owns the `apm.yml`.

See also: [`apm init` reference](../../reference/cli/init/)

### `[!] apm.yml uses the deprecated 'target:' key`

```
[!] apm.yml uses the deprecated 'target:' key; 'target:' will be removed in a future release in favor of 'targets:'. Replace 'target: claude,copilot' with 'targets: [claude, copilot]'.
```

Cause: the project `apm.yml` (or `~/.apm/apm.yml`) declares the singular `target:` key. It still works, prints once per run on stderr, and does not change the exit code.

Fix: make the replacement the warning prints and commit it. It deploys the same files. If the warning says `targets:` has no exact equivalent (for example `target: intellij`), write a `targets:` list of the targets you use and pass `--target intellij` to `apm install`.

See also: [Manifest schema: targets](../../reference/manifest-schema/#36-target-and-targets)

### `` [!] apm.yml: replace `vscode` with `copilot` ``

```
[!] apm.yml: replace `vscode` with `copilot` in 'targets:'; `vscode` is deprecated and will be removed in a future release.
```

Cause: the project `apm.yml` or `~/.apm/apm.yml` uses `vscode` under `targets:` or a dependency's `targets:` (under `target:`, the `target:` warning above covers it). `--target vscode` and `--runtime vscode` print the same warning prefixed with the flag (`--target: replace ...`). `vscode` selects exactly what `copilot` selects; the exit code does not change.

Fix: replace `vscode` with `copilot`.

### `[!] Kept MCP server '<name>' in .github/mcp.json`

```
[!] Kept MCP server 'my-server' in .github/mcp.json because it was changed after APM wrote it. Copilot now uses the 'my-server' in .mcp.json, which APM writes from apm.yml; put your changes in apm.yml and delete this entry.
```

Cause: Copilot project MCP servers now live in the root `.mcp.json`, which takes precedence over `.github/mcp.json`. On upgrade, `apm install` moves the entries an older APM wrote to `.github/mcp.json` or `.vscode/mcp.json`; entries you edited stay put, but Copilot now uses the `.mcp.json` copy.

Fix: move your changes into the server's declaration in `apm.yml`, run `apm install`, and delete the entry from the named file.

See also: [Install MCP servers: Copilot project file](../../consumer/install-mcp-servers/#how-targets-gates-which-configs-get-written)

### `<path>/apm.yml uses the YAML anchor '&<name>' on line <n>`

```
[x] /home/me/project/apm.yml uses the YAML anchor '&shared' on line 4. apm.yml must not use YAML anchors (&name) or aliases (*name). Write the value out in full wherever it is used, then retry.
```

Cause: `apm.yml` defines a YAML anchor (`&name`) or reuses one with an alias (`*name`), including `<<: *name` merge keys. OpenAPM manifests forbid both, so `apm install`, `apm compile`, `apm audit`, `apm init` and every other command that reads the manifest refuse it before changing any file. The message names the file and the first offending line; the same error reports an alias as `YAML alias '*<name>'`.

Fix: delete the `&name` marker and replace each `*name` with a copy of the value it pointed to, then re-run the command.

See also: [Manifest schema](../../reference/manifest-schema/)

### `'intellij' is an IDE, not a target, so apm.yml cannot record it`

```
[x] 'intellij' is an IDE, not a target, so apm.yml cannot record it. Use '--target copilot' for GitHub Copilot in JetBrains IDEs; once apm.yml exists, '--target intellij' writes JetBrains MCP config.
[i] Nothing was written.
```

Cause: a command that creates or rewrites `apm.yml` (`apm init --target`, or `apm install <pkg> --target` / `apm install -g --mcp NAME --target` with no manifest yet) was given `intellij`. `intellij` selects JetBrains Copilot's user-scope MCP config; it is not a `targets:` value, so APM stops before writing anything.

Fix: use `--target copilot` for GitHub Copilot in JetBrains IDEs. To also write JetBrains MCP config, create `apm.yml` first (`apm init`), then run `apm install --mcp NAME --target intellij`.

See also: [`apm install`](../../reference/cli/install/)

### `lockfile not found at <path>; run 'apm install' to generate it`

```
lockfile not found at apm.lock.yaml; run 'apm install' to generate it
```

Cause: a command that needs a resolved dependency graph (drift check, audit, compile against lockfile) ran before the project had ever been installed.

Fix: run `apm install` once to produce `apm.lock.yaml`, then re-run the original command.

See also: [Install failures](../install-failures/), [Lockfile specification](../../reference/lockfile-spec/)

### `<pkg>: not found in lockfile`

(message similar to)

```
<owner>/<repo>: not found in lockfile
1 ref mismatch -- run 'apm update' to update lockfile
```

Cause: a dependency exists in `apm.yml` but has no entry in `apm.lock.yaml`. Most often hit in CI when the lockfile was not regenerated after editing the manifest.

Fix: run `apm install` locally to refresh `apm.lock.yaml` and commit the result. Do not hand-edit the lockfile.

See also: [Install failures](../install-failures/), [Lockfile specification](../../reference/lockfile-spec/)

### `<pkg>: manifest ref '<ref>' != lockfile ref '<ref>'`

```
<owner>/<repo>: manifest ref 'v2' != lockfile ref 'v1'
N ref mismatches -- run 'apm install' to update lockfile
```

Cause: the version or ref pinned in `apm.yml` was changed without re-running install. The lockfile is stale.

Fix: run `apm install` and commit the updated `apm.lock.yaml`.

See also: [Install failures](../install-failures/)

### `Drift detected: N file(s)`

```
[!] Drift detected: N file(s)
[i] To restore the locked content, delete the modified files and rerun 'apm install', then commit the result ('apm install --force' also restores them, but overwrites every local edit and deploys despite critical security findings).
```

When some files are missing or unrecorded instead, APM prints `Run 'apm install' to re-sync deployed files with the lockfile.`

Cause: deployed primitive files in the project differ from the content recorded in `apm.lock.yaml`. Either the files were edited locally, or the lockfile was updated without re-running install.

Fix: for missing or unrecorded files, run `apm install`. Plain `apm install` can keep a local edit to a managed file, so for `modified` files delete them, rerun `apm install`, and commit the result. `apm install --force` also restores them, but it overwrites every local edit and deploys despite critical security findings. If the local edits were intentional, vendor them into the source package or a local override before re-installing.

For `unmanaged-conflict`, the file is yours, not a managed edit: install skipped
it (or refused the skill plan). Rename the conflicting local file or skill, or
choose a valid existing skill/target selection, first; `apm install --force`
overwrites a skipped non-skill file. Audit does not adopt, overwrite, or delete the local file.

See also: [Install failures](../install-failures/)

### `Content hash mismatch for <pkg>: expected <a>, got <b>`

```
[x] Content hash mismatch for <owner>/<repo>: expected <sha>, got <sha>.
The downloaded content differs from the lockfile record. This may
indicate a supply-chain attack. Use 'apm update <owner>/<repo>' to accept
new content for this package and re-pin it in the lockfile.
```

At user scope the hint names `apm update -g <owner>/<repo>`. For a transitive dependency it names the full `apm update`, since only direct dependencies can be targeted.

Cause: the bytes APM downloaded for a locked dependency do not match the `content_hash` recorded in `apm.lock.yaml`. Usually a force-pushed tag, a mutated branch, or a tampered mirror. Older APM versions on Windows also recorded CRLF hashes when Git's host settings (`core.autocrlf=true`, or `core.eol`, which defaults to CRLF on Windows, for `.gitattributes` `text=auto` files) expanded LF-committed files. Current APM pins `core.autocrlf=false` and `core.eol=lf` on its dependency checkouts and rematerializes older cache shards, but `--frozen` cannot rewrite a lockfile that already stored that CRLF hash.

Fix: investigate the upstream package before accepting. If the change is legitimate, run the command the hint names (`apm update <owner>/<repo>`) to re-pin and commit the new hash. `apm update` can advance mutable refs, so review the SHA and `content_hash` diff before committing. If the lockfile was generated by a pre-fix Windows CLI, rematerialize the affected package from a fresh consumer workspace (or delete the cached/installed copy) and regenerate with `apm update` or `apm lock --update`. Bare `apm lock` does not rewrite an existing CRLF hash.

See also: [Install failures](../install-failures/), [Lockfile specification](../../reference/lockfile-spec/)

### `Install blocked by org policy -- see violations above`

```
[x] Install blocked by org policy -- see violations above
```

Cause: an org-level policy (compilation target, dependency allowlist, security baseline) rejected one or more dependencies. The per-violation lines printed above this message identify the offending packages and rules.

Fix: read the violations above the summary line, remove or replace the offending packages, or update org policy if the rule is wrong.

See also: [Policy debugging](../policy-debugging/)

### `from unapproved host(s): <host>`

(message similar to)

```
[x] Re-run with --allow-insecure-host <host> to allow transitive HTTP dependencies from unapproved host(s): <host>.
```

Cause: a transitive dependency uses an `http://` URL on a host you have not approved for this install.

Fix: switch the dependency to HTTPS, or, if you trust the host, re-run with `--allow-insecure-host <host>` (repeat per host). Do not bypass this check in CI.

See also: [Policy debugging](../policy-debugging/)

### `Race condition: concurrent checkout failed integrity for <url> @ <sha>`

```
Race condition: concurrent checkout failed integrity for <url> @ <sha>
```

Cause: two `apm install` processes raced on the same cache shard and the loser's integrity check failed. The cache shard lock is per-shard with a 120s default timeout.

Fix: re-run `apm install`. If it reproduces, ensure only one install runs at a time per `APM_CACHE_DIR`. In CI, give each job its own cache directory.

See also: [Install failures](../install-failures/)

### `Refusing to run '<command>' on apm.lock.yaml: it was written by APM <version>`

```
Refusing to run 'apm install' on apm.lock.yaml: it was written by APM 0.38.0, newer than this APM 0.37.0. An older APM can drop or misread what a newer one recorded. Nothing was changed.
Install APM 0.38.0 or newer. Upgrade, then retry:
  apm self-update                            (installer script)
  brew upgrade apm                           (Homebrew)
  python3 -m pip install --upgrade apm-cli   (pip)
```

Cause and fix: a newer APM wrote the lockfile. There is no override; upgrade APM and retry. See [Lockfile written by a newer APM](../install-failures/#lockfile-written-by-a-newer-apm).

### `marketplace '<name>' now points at <origin>, but apm.lock.yaml recorded ...`

```
marketplace 'company' now points at <new-origin>, but apm.lock.yaml recorded 'example-plugin' from <locked-origin>. Nothing was changed. To keep the locked catalog, run: apm marketplace add <locked-origin> --name company ...
```

Cause: the alias was re-registered for a different repository than the one `apm.lock.yaml` recorded. Any command that re-resolves a locked entry through the marketplace (such as `apm update`, `apm install --refresh`, `apm install` after you change the entry in `apm.yml`, or a fresh-checkout install of a package that contributes MCP or LSP servers) refuses to switch marketplaces silently.

Fix: run the `apm marketplace add` command the message prints to restore the locked origin. To switch on purpose, follow the remove, `apm install`, re-add steps it lists.

### `<a> and <b> are different packages that would both install to apm_modules/<path>`

```
github.com/acme/tool and gitlab.example.com/acme/tool are different packages that would both install to apm_modules/acme/tool. Remove one of them, or set 'alias:' on one so it installs to its own directory.
```

Cause: the same `owner/repo` is declared from two hosts (or two dependencies share an `alias:`). A transitive declaration is labelled `(required by <pkg>)`. APM stops before downloading either.

Fix: remove one declaration, or set `alias:` on one.

### `refused (dependency-confusion risk #1326): bare repo: ... on enterprise marketplace`

```
refused (dependency-confusion risk #1326): bare `repo: acme/tool` on enterprise marketplace 'corp.ghe.com' is ambiguous. Host-qualify the plugin `repo` field in marketplace.json to one of:
  - 'corp.ghe.com/acme/tool' (enterprise dep on this marketplace)
  - 'github.com/acme/tool' (declared cross-host dep on public github.com)
```

Cause: a plugin on a `*.ghe.com` marketplace declares a bare cross-repo `repo:`, which would otherwise resolve to public `github.com`. APM refuses it whenever it resolves the entry through the marketplace (CLI argument, `apm.yml` entry, transitive dependency, `apm update`), before downloading or probing the plugin repository. Rows replayed from `apm.lock.yaml` skip this check; `apm update` re-resolves them through it.

Fix: the marketplace maintainer host-qualifies `repo:` in `marketplace.json` as the message shows.

### `Could not verify that marketplace '<name>' is current`

```
Could not verify that marketplace 'acme' is current: <reason>. The cached catalog was kept and nothing was changed. Retry when the catalog is reachable; a plain 'apm install' still uses the cached catalog.
```

Cause: `apm install --refresh`, `apm update`, and `apm marketplace update` revalidate each marketplace they read and fail rather than use cached data.

Fix: restore network access or credentials for the marketplace and retry.

### `--frozen: dependency selections differ from apm.lock.yaml.`

```
[x] --frozen: dependency selections differ from apm.lock.yaml.
  - selection for <owner>/<repo> changed: the skills and targets its declaring manifests request differ from apm.lock.yaml; run 'apm install' and commit apm.lock.yaml
[i] Tip: run 'apm install' without --frozen, retaining --root DIR or --global if specified, then commit apm.lock.yaml.
```

Cause: several manifests narrow one dependency with different `skills:` or `targets:`, and `apm.lock.yaml` records another pairing (lockfiles from APM before 0.37 record the cross-product). `--frozen` refuses before deploying anything.

Fix: run `apm install` without `--frozen` once and commit `apm.lock.yaml`.

### `MCP server '<name>' conflicts with the existing <runtime> connection`

```
[x] MCP server 'synthdb' conflicts with the existing claude connection in <project>/.mcp.json. Preserved that connection. Field args[0] holds a value where APM writes a package path, and APM cannot confirm it wrote it. Restore the package path APM writes (args[0]: ${CLAUDE_PROJECT_DIR:-.}/apm_modules/...) or rename or remove the connection yourself, then retry.
    --force does not take ownership of user connections.
```

Cause: an entry in `.mcp.json` (or another native MCP config) with the same name was not written by APM, or was edited after APM wrote it. `--force` does not take ownership of it.

Fix: restore the exact value the message names (keep the `:-.` default), or rename or remove the connection, then re-run the same install. Rotate credentials only when the hint names `apm.yml` environment references.

## Compile

### `Compilation completed but produced no output files`

Printed when compile produces zero output.

Cause: no primitives in the project (or filtered subset) match the active target's compilation rules. Common causes: wrong `--target`, missing frontmatter, glob filter matched nothing, or org policy stripped everything.

Fix: run `apm compile --dry-run --verbose` to see what was discovered and why each primitive was skipped.

See also: [Compile zero-output warning](../compile-zero-output-warning/), [`apm compile` reference](../../reference/cli/compile/)

Target-resolution errors print with `Error:` or `[x]` in front, depending on the command.

### `No harness detected`

```
[x] No harness detected
```

Cause: the project has no harness markers (`.github/copilot-instructions.md`, `.claude/`, `.cursor/`, ...) and no `targets:` block in `apm.yml`. APM no longer defaults to copilot.

Fix: pass `--target <harness>` on the command, or declare `targets:` in `apm.yml`. Run `apm targets` to list supported harnesses.

See also: [`apm targets` reference](../../reference/cli/targets/), [Targets matrix](../../reference/targets-matrix/)

### `Multiple harnesses detected: <a>, <b>`

Cause: APM found markers for more than one harness and cannot pick.

Fix: pin a target with `--target <harness>` or declare `targets:` in `apm.yml`. Use `apm install --dry-run` to preview each option.

See also: [`apm targets` reference](../../reference/cli/targets/)

### `Unknown target '<value>'`

```
Error: Unknown target '<value>'

Valid targets: claude, copilot, cursor, ...
```

Cause: the value passed to `--target` (or listed in `apm.yml` `targets:`) is not a known harness.

Fix: run `apm targets` for the canonical list. Check for typos.

See also: [`apm targets` reference](../../reference/cli/targets/), [Targets matrix](../../reference/targets-matrix/)

## Audit

### `Policy violation: <dep> -- <reason>`

```
[x] Policy violation: <owner>/<repo>@<ref> -- <reason>
```

Cause: a dependency failed an org policy check during install or audit. The reason field names the rule.

Fix: see [Policy debugging](../policy-debugging/) for resolving each rule type. For audit-only reporting, use `apm audit --format sarif` or `--format json` to feed the findings into your code-scanning pipeline.

See also: [Policy debugging](../policy-debugging/), [`apm audit` reference](../../reference/cli/audit/)

### `Policy hash mismatch from <source>: expected <a>, got <b>`

Cause: the org policy file fetched from the configured source does not match the pinned hash. Either the policy was updated upstream and the pin is stale, or the source was tampered with.

Fix: confirm the upstream change is legitimate, then update `policy.hash` in `apm.yml` to the new hash.

See also: [Policy debugging](../policy-debugging/)

## Run

### `Script or prompt '<name>' not found.`

```
Script or prompt '<name>' not found.
Available scripts in apm.yml: <list>
```

Cause: `apm run <name>` could not match `<name>` to a script in `apm.yml`, an auto-discovered prompt file, or an installable virtual package.

Fix: check the available scripts list printed under the error (`apm list` shows the same scripts). The error also lists where APM looks for prompts. To install a remote prompt, use `apm install <owner>/<repo>/path/to/prompt.prompt.md`.

See also: [`apm run` reference](../../reference/cli/run/), [`apm list` reference](../../reference/cli/list/)

### `No such command '<x>'`

```
Error: No such command '<x>'.
```

Cause: standard Click error. The subcommand does not exist, or you are on an older APM version that lacks it.

Fix: run `apm --help` for the current command list. Upgrade APM if the command was added in a newer release.

See also: [Reference](../../reference/), [Troubleshooting migration](../migration/)

## Auth and network

### `GitHub API throttle for <host> (HTTP <status>)`

```
GitHub API throttle for github.com (HTTP 429)
```

Cause: GitHub conclusively throttled a virtual-file API probe. APM treats this
as unknown rather than claiming the dependency is missing. It makes one sparse
Git attempt; a later `sparse Git transport failed` message identifies a
separate Git connectivity or authorization problem.

Fix: for public repositories, retry after the GitHub quota recovers if the
sparse Git attempt also failed. For private repositories, configure the normal
GitHub credential chain (for example `GITHUB_APM_PAT`) and confirm that the
token can read the repository. A `401`, ordinary `403`, or `404` is not a
throttle and requires fixing authentication, authorization, or the dependency
path instead.

See also: [Install failures](../install-failures/), [Authentication](../../getting-started/authentication/)

### `Authentication failed for <operation> on <host>`

```
Authentication failed for <operation> on <host>.
No token available.
Set GITHUB_APM_PAT or GITHUB_TOKEN, or run 'gh auth login'.
```

Cause: the remote host rejected APM's credentials, or no credentials were available. Token resolution order for GitHub: per-org `GITHUB_APM_PAT_<ORG>`, then `GITHUB_APM_PAT`, then `GITHUB_TOKEN`, then `GH_TOKEN`, then `gh auth token`, then git credential helpers.

Fix: set `GITHUB_APM_PAT` (or run `gh auth login`) with a token that has access to the dependency's repo. For Azure DevOps, set `ADO_APM_PAT`. The diagnostic block printed under the error names the host and the resolution attempts that failed.

See also: [Install failures](../install-failures/)

### `TLS verification failed`

```
TLS verification failed -- APM uses the system trust store by default.
If you're behind a corporate proxy or firewall, make sure your
organisation's CA is installed in the OS trust store, or set
APM_EXTRA_CA_BUNDLE to a readable PEM bundle to add it while retaining
public trust. Use REQUESTS_CA_BUNDLE only to replace the complete
Requests trust set.
```

Cause: Python's TLS stack rejected the server certificate. Almost always a corporate proxy doing TLS interception with a CA that is not in the system trust store.

Fix: install the corporate CA into the OS trust store and retry. For a per-shell additive setting that retains public trust, export `APM_EXTRA_CA_BUNDLE=/path/to/corporate-ca.pem`. Use `REQUESTS_CA_BUNDLE` only when you intend to replace the complete Requests trust set; `SSL_CERT_FILE` alone is not a reliable requests override. Do not disable TLS verification.

If the hint names `APM_DISABLE_TRUSTSTORE`, `REQUESTS_CA_BUNDLE`, or `CURL_CA_BUNDLE` instead, that setting is active; follow the hint's advice for that trust mode.

See also: [SSL issues](../ssl-issues/)

### Network timeout or host unreachable

(no fixed string; surfaces as `requests` / git transport errors after a timeout)

Cause: the host is unreachable, the proxy is down, or DNS is failing.

Fix: confirm `curl -v https://<host>` works from the same shell. Check `HTTPS_PROXY` / `NO_PROXY` are set correctly. Re-run `apm install --verbose` to see which host failed and on which leg (API probe vs git fetch).

See also: [SSL issues](../ssl-issues/), [Install failures](../install-failures/)
