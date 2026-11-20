---
title: Agent supply chain security model
description: What APM controls in the agent supply chain, what it does not, its permissive defaults, how to harden them, and who owns each remaining risk.
---

This page is for the security reviewer who has to sign off on APM. It lists every
control APM provides, the defaults you start from, the threats each control answers,
and what stays your responsibility. For why agent files are a supply chain at all,
read [The agent supply chain](../../concepts/agent-supply-chain/).

## The question

A coding agent reads files from your repository: skills, instructions, prompts, agents,
hooks, MCP and LSP server configuration, plugins. A harness, the agent tool that reads them
(GitHub Copilot, Claude Code, Cursor, Codex and others), picks these files up as soon as they are on disk; there is no
separate "run" step. Hooks and MCP servers also start processes on the developer's
machine. So the question is: **what may land in those folders, who decided, and can you
show it later?**

## Short answer

APM controls what gets installed: it declares, locks, verifies, scans and gates agent
capabilities against an org policy, and exports an inventory. It does not control what
runs: your harness does. APM is not a sandbox, verifies no signatures, and its lockfile
records what arrived, not whether it deserves trust: whatever APM saw at adoption
becomes the baseline.

APM checks in two layers:

1. **Built-in protection (automatic).** `apm install` blocks critical hidden-Unicode
   findings before files reach agent folders, refuses downloads whose content hash
   differs from the lockfile, and applies org policy. `apm compile` and `apm pack` scan
   their output too. Zero configuration.
2. **`apm audit` (explicit).** Reporting (SARIF, JSON, markdown), remediation
   (`--strip`), standalone file scanning (`--file`), and the CI gate (`--ci`) that
   replays the lockfile and re-checks policy.

## The controls

| Control | What APM does | What it does not do | Command |
|---|---|---|---|
| Declare | `apm.yml` lists every package and MCP server, once, for all harnesses | Judge whether a package is good | `apm install <pkg>` |
| Lock | `apm.lock.yaml` records the resolved commit, a SHA-256 `content_hash` per package and a hash per deployed file; marketplace installs also record which marketplace each package came from | Vouch for the initial download | `apm install`, `apm lock` |
| Verify | A download that differs from `content_hash` aborts the install; `--frozen` refuses a lockfile out of sync with `apm.yml` and deployed files that differ from their hashes | Verify signatures (no Sigstore, in-toto, SLSA or GPG) | `apm install --frozen` |
| Scan | Hidden Unicode (36 character ranges, plus mid-file BOMs) at install, compile, pack and audit; critical findings block deployment | Detect malware, visible prompt injection or misleading text | `apm audit` |
| Approve | Once the executables gate is on, hooks, MCP servers, LSP servers, `bin/` and canvas from dependencies are parked until approved | Sandbox approved code, or cover code a hook downloads at run time | `apm approve` |
| Govern | Org policy allows or denies sources, MCP servers, transports and harnesses; inheritance tightens, never loosens; enforced at install and in `apm audit --ci` | Block anything under the default `warn`; govern user-scope (`-g`) installs | `apm audit --ci --policy org` |
| Detect drift | Replays the lockfile in a scratch folder and diffs the working tree | Watch files between runs; judge files APM did not write | `apm audit` |
| Inventory | CycloneDX 1.5 or SPDX 2.3 from the lockfile; `apm find` traces a file to its package | Attest anything: it is an inventory, not an attestation | `apm lock export` |

## Defaults

Every default is permissive so adoption does not break teams. Harden them on purpose.

| Setting | Default | What happens |
|---|---|---|
| Policy `enforcement` | `warn` | Violations print `[!]`; installs and `apm audit --ci` still pass |
| Policy `fetch_failure` | `warn` | If a cached policy (up to 7 days old) cannot be refreshed, APM proceeds with the cached copy. With no usable cache, APM proceeds without a policy unless the repository sets `policy.fetch_failure_default: block` |
| Executables | Allowed | Hooks, MCP and LSP servers, `bin/` and canvas deploy until the project declares an `executables:` block (formerly `allowExecutables`) or the org policy carries a non-empty `executables:` block |
| `security.integrity.require_hashes` | `false` | Lockfile entries without a content hash are accepted |

### Hardening checklist

- Org policy: `enforcement: block`, `fetch_failure: block`,
  `security.integrity.require_hashes: true`. See [Set an org policy](../apm-policy/).
- Each repository's `apm.yml`: `policy.fetch_failure_default: block`, so a missing or
  unreachable policy fails closed, and optionally `policy.hash` to pin the policy bytes.
- Executables gate on, with an organization `recommend` list. See [Control executables](../control-executables/).
- A required pull-request check that runs `apm policy status --check`,
  `apm install --frozen` and `apm audit --ci --policy org`. See [Gate pull requests](../enforce-in-ci/).
- CODEOWNERS and branch protection on the repository that holds `apm-policy.yml`.
- A review rule for workflow changes that add `--no-policy`, `APM_POLICY_DISABLE`,
  `--force`, `--trust-transitive-mcp` or `--allow-insecure`.

## Threat model

Each row names what an attacker wants, the control that answers it, and who owns the
risk that remains.

| Attacker goal | Control | Residual risk owner |
|---|---|---|
| Change a package after you adopted it (moved tag, force-push, swapped archive) | Commit pin plus `content_hash`; a mismatching download aborts; `--frozen` in CI | You, at adoption: the lockfile trusts what it saw then |
| Get a malicious package or version adopted | Policy allow and deny lists; lockfile diffs in pull requests; approvals for executables | Your reviewers: APM does not judge content |
| Hide instructions in invisible characters | Hidden-Unicode scan blocks critical findings at install; `apm audit` reports and strips | Your reviewers, for visible prompt injection |
| Run code on developer machines through hooks or MCP servers | Executables gate and approvals | The harness, which governs what runs and with which permissions |
| Substitute a same-named package from another host (dependency confusion) | Host-qualified policy patterns such as `github.com/contoso/**`; `*.ghe.com` marketplaces refuse bare cross-repository sources | You: host-qualify allow patterns |
| Skip the policy on a laptop | `--no-policy` and `APM_POLICY_DISABLE=1` are local; the required CI check runs anyway | You: branch protection makes the check required |
| Weaken or spoof the org policy | Inheritance never loosens; cross-host `extends:` rejected; an incomplete chain fails closed; `policy.hash` pin | Owners of the policy repository |
| Edit deployed agent files outside APM | Drift replay and `content-integrity` in `apm audit --ci`; `--frozen` | You, for files APM did not deploy (see `unmanaged_files` in the [policy schema](../../reference/policy-schema/#unmanaged_files)) |

:::caution[What APM does not do]
- **No signatures.** APM verifies no package, commit or release signatures.
- **Not an attestation.** A lockfile or SBOM is a record of what was installed.
- **No malware or semantic scanning.** The content scan covers hidden Unicode and nothing else.
- **Not a sandbox.** Policy decides what gets installed. The harness decides what runs
  and with which permissions; configure it there.
- **Project installs, not user installs.** Org policy is discovered from the repository's git remote, so
  user-scope installs (`apm install -g`) are not governed by it.
:::

## HTTPS transport trust

APM keeps certificate verification on for every HTTPS request. Python-based paths verify
against the operating system's trust store through `truststore`, so corporate roots that
`git` and `curl` trust are trusted by `apm install` too.

- `REQUESTS_CA_BUNDLE` takes precedence over `CURL_CA_BUNDLE`; either one replaces normal
  trust and turns off the OS and additive certificates.
- `APM_DISABLE_TRUSTSTORE=1` turns off OS and additive trust without unsetting a replacement
  bundle you configured separately.
- `APM_EXTRA_CA_BUNDLE` adds PEM certificates to APM's package-management HTTPS and keeps the
  native roots. A selected bundle that is missing, unreadable, empty, over 8 MiB, malformed or
  contains a private key fails closed.
- The Python `llm` runtime gets its own `.pth` OS-trust bootstrap at venv setup;
  `APM_EXTRA_CA_BUNDLE` does not extend that bootstrap.

Git, the Node-based Copilot CLI and the Rust-based Codex CLI keep their own trust settings.
See [SSL / TLS issues](../../troubleshooting/ssl-issues/).

## HTTP (insecure) dependencies

APM installs `http://` git dependencies, for private mirrors and offline networks, when two
explicit approvals are both present: `allow_insecure: true` on the dependency in `apm.yml`, and
`apm install --allow-insecure` for the run. Transitive HTTP dependencies from another host
also need `--allow-insecure-host HOSTNAME`. APM never sends stored git credentials over HTTP.

These approvals make the decision visible; they do not protect the connection. HTTP has no
encryption or server authentication, and the initial lockfile entry comes from that same
channel.

Treat insecure transport as its own CI control. `apm-policy.yml` has no dedicated
`allow_insecure` field: `dependencies.allow` and `dependencies.deny` can name a host (see
[dependency patterns](../../reference/policy-schema/#dependencies)) but match scheme-blind, so
they cannot distinguish `http://` from `https://` for the same package. An allow list
therefore does not enforce HTTPS. Reject committed `allow_insecure: true` entries in review,
and keep `--allow-insecure` and `--allow-insecure-host` out of standard CI.
`registry_source.allow_non_registry` is a separate source-routing control, not an
insecure-transport setting.

## Content scanning

`apm install` scans the files it is about to deploy, before any reaches an agent folder.
Critical findings (tag characters, bidirectional overrides, variation selectors 17-256,
unpaired surrogates)
block that package and the command exits 1; other packages still install. Warnings
(zero-width characters, bidirectional marks) deploy and are reported. `--force`
overrides a block. `apm audit` re-scans deployed files, `apm audit --file <path>` scans
any file, and `apm audit --strip` removes hidden characters from deployed copies.
Check names and severities: [Baseline CI checks](../../reference/baseline-checks/).

## Inventory export (SBOM)

`apm lock export --format cyclonedx|spdx` serializes what `apm.lock.yaml` already
recorded. It never re-resolves, re-hashes or touches the network. Licenses are the
`license:` each package declares in its `apm.yml`, validated against SPDX identifiers;
APM never reads `LICENSE` files, and an undeclared license is `NOASSERTION` in SPDX and
omitted in CycloneDX. See
[Inventory and drift](../drift-detection/).

## Source and license

APM is developed in the open at
[github.com/microsoft/apm](https://github.com/microsoft/apm); the source, build pipeline
and release process are public. APM-owned software, bundled agent primitives, code
examples and the software-facing OpenAPM specification are licensed under
[Apache-2.0](https://github.com/microsoft/apm/blob/main/LICENSE). General documentation
is licensed under
[CC-BY-4.0](https://github.com/microsoft/apm/blob/main/LICENSE-DOCS).
[LICENSING.md](https://github.com/microsoft/apm/blob/main/LICENSING.md) defines the
scope and exclusions; Markdown agent primitives are software, not general documentation.
Third-party material retains its license; attribution and the historical MIT notice are
in [NOTICE](https://github.com/microsoft/apm/blob/main/NOTICE). APM collects no
telemetry.
