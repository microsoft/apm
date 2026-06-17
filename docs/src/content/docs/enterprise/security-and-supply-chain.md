---
title: Security and Supply Chain
description: APM's threat model and the mechanisms that back it -- integrity, provenance, secrets, scanning, policy gates -- and what APM does not defend against.
sidebar:
  order: 7
---

This page is for the admin or security reviewer who has to sign off on
APM. It states the threat model in one paragraph, then walks the
mechanisms in the source that back it -- and is equally explicit about
what APM does not defend against.

## Threat model

APM defends the **build-time supply chain** for AI agent context:
prompts, instructions, skills, hooks, and MCP server declarations
flowing from a git source through `apm install` into your project tree
and on into seven harnesses. The defended properties are reproducibility
(same install everywhere), integrity (downloaded content matches the
lockfile), provenance (every dep traces to a pinned commit at a named
host), and pre-deploy content safety (no hidden Unicode reaches the
agent). APM does **not** sandbox MCP servers at runtime, does not do
malware analysis on dependency code, does not sign packages, and does
not inspect what an agent does once it has read your context. See
[The three promises](../concepts/the-three-promises/) for the
canonical framing.

## Integrity

Three layers, in order of when they fire:

**Lockfile pinning.** `apm.lock.yaml` records the full 40-char
`resolved_commit` for every dep and the `content_hash` (SHA-256 over
the sorted file tree) of what was installed. Source:
`src/apm_cli/deps/lockfile.py`.

**Fresh-download hash check.** When APM downloads a package whose
lockfile entry already has a `content_hash`, it recomputes the hash
post-download and aborts the install on mismatch -- the partial
download is removed and the user is told to use `apm install --update`
if the change is intentional. Source: `src/apm_cli/install/sources.py`
(`content_hash` mismatch handling around lines 770-784).

**Cache-hit verification.** On every cache HIT, APM reads the cached
checkout's `.git/HEAD` and compares it to the lockfile's
`resolved_commit`. The `.git/HEAD` file is read directly rather than
spawning `git rev-parse`, so a poisoned `.git/config` cannot subvert
the check. On mismatch the cache entry is evicted and a fresh fetch
runs. Source: `src/apm_cli/cache/integrity.py:74` (`verify_checkout_sha`).

Bundles (the local-file install path used by `apm pack` outputs) get a
fourth check: every file listed in `pack.bundle_files` is SHA-256
verified, symlinks anywhere under the bundle root are rejected, and
files not listed in the manifest are flagged as a tampering signal.
Source: `src/apm_cli/bundle/local_bundle.py:287-368`
(`verify_bundle_integrity`).

## Provenance

Dependencies are git URLs. There is no central registry to compromise.
Every lockfile entry records `host`, `repo_url`, `resolved_ref`, and
`resolved_commit`, so "where did this file come from?" is answered by
one `grep` of `apm.lock.yaml`.

HTTP (cleartext) git deps require explicit opt-in on both the manifest
(`allow_insecure: true` per dep) and the CLI (`apm install
--allow-insecure`). For HTTP fetches APM suppresses git credential
helpers so stored tokens are never sent in the clear. HTTP makes the
first fetch's hash and commit untrusted-by-channel even though replay
detection remains intact -- treat HTTP deps as "I trust the network
path" assertions, not as "APM made this safe".

For the registry-proxy / air-gap story see
[Registry proxy](./registry-proxy/).

## Inventory export (SBOM)

`apm lock export --format cyclonedx|spdx` serializes the lockfile into a
CycloneDX 1.5 or SPDX 2.3 document. This is an **inventory** export, not a
security attestation: it reflects exactly what `apm.lock.yaml` already
recorded -- component identity (purl), recorded hashes, and the declared
license -- and never re-resolves, re-hashes, or touches the network or the
filesystem. Source: `src/apm_cli/export/`.

Component identity is a Package URL: `pkg:github/<owner>/<repo>@<commit>`
for git deps (the forge is read from the lockfile, falling back to
`pkg:generic/` for an unrecognized host), `pkg:oci/<name>@<digest>` for
registry deps, and `pkg:generic/<name>@<content_hash>` for local
primitives. Output is deterministic -- components sorted by purl, a pinned
timestamp (`--timestamp`, then `SOURCE_DATE_EPOCH`, then the lockfile's
`generated_at`), stable key order -- so two runs are byte-identical. Any
credentials embedded in a recorded URL -- userinfo (`user:pass@`) or
query-string tokens (`?access_token=`, SAS `?sig=`) -- are scrubbed before
they reach the document.

**Declared license (manifest-declared, never concluded).** APM records the
license the package *manifest declares* (`license:` in `apm.yml`, or
`license` in a `plugin.json`) into the lockfile's `declared_license` field at
resolve time, syntax-validates it offline against the bundled SPDX id set, and
passes it through to the SBOM. APM never reads or interprets the text of a
`LICENSE` file -- declared is not concluded. Three states, never collapsed:

| State | Lockfile | CycloneDX | SPDX |
|---|---|---|---|
| Declared, valid SPDX (`MIT`, `(MIT OR Apache-2.0)`) | verbatim | `license.id` / `expression` | the value |
| Declared special token (`UNLICENSED`, `SEE LICENSE IN <f>`) | verbatim | `license.name` (a named assertion) | the value |
| Not declared | field omitted | `licenses[]` omitted | `NOASSERTION` |

The not-declared state is genuinely unknown, never silently upgraded to a
license just because a `LICENSE` file exists on disk. On the **authoring**
path (`apm pack` / `apm publish` on your own `apm.yml`) a missing `license:`
prints an actionable warning; on the **consuming** path (install/export of
other people's deps) APM stays silent -- it never nags about transitive
licenses it cannot fix. Source: `src/apm_cli/export/authoring.py`.

See [`apm lock export`](../../reference/cli/lock/#export-sbom-inventory) and
the [`license` manifest field](../../reference/manifest-schema/#35-license)
for the per-command reference.

## Secret handling

APM has no secret store. The contract is:

- **Tokens come from the environment.** `GITHUB_APM_PAT` for GitHub
  hosts, `ADO_APM_PAT` for Azure DevOps. Tokens are scoped per host
  family and never forwarded cross-host. See
  [Authentication](../consumer/authentication/).
- **MCP `env:` blocks in `apm.yml` are name/value pairs**, intended to
  hold *references* (e.g. `GITHUB_TOKEN: ${GITHUB_TOKEN}`) that the
  harness resolves at agent runtime, not literal secrets. Source:
  `src/apm_cli/install/mcp/entry.py`,
  `src/apm_cli/integration/mcp_integrator_install.py` (orchestration) and
  `src/apm_cli/integration/mcp_integrator.py` (runtime wiring).
- **`apm install` (project scope) writes `apm_modules/` to `.gitignore` automatically**
  on first install. Source:
  `src/apm_cli/commands/_helpers.py:414` (`_update_gitignore_for_apm_modules`).
  This keeps cached source trees out of commits. Global installs (`apm install -g`)
  do **not** modify `.gitignore` in the current working directory.
- **`apm.yml` is committed; `.env` is yours.** APM never reads `.env`
  files itself; that is delegated to the agent harness.

Recommendation: scan committed `apm.yml` files in CI for literal
secrets in `mcp.env` values -- APM does not enforce env-var indirection,
it only assumes it.

## Content scanning

Agent context is executable for an LLM. APM scans for hidden Unicode
that humans cannot see on screen but a tokenizer reads as instructions.

`ContentScanner` (`src/apm_cli/security/content_scanner.py`)
classifies each codepoint as `critical`, `warning`, or `info`:

| Severity | Examples | Why |
|---|---|---|
| critical | Tag chars (U+E0001-E007F), bidi overrides (U+202A-E, U+2066-9), variation selectors 17-256 (U+E0100-E01EF) | No legitimate use in prompt files. Glassworm-class hidden-payload vectors. |
| warning | Zero-width spaces/joiners (U+200B-D), bidi marks, invisible math operators, deprecated formatting | Common copy-paste debris that *can* hide content. ZWJ inside an emoji sequence is downgraded to info. |
| info | Non-breaking spaces, leading BOM, emoji presentation selector | Logged for awareness, not actioned. |

`SecurityGate` (`src/apm_cli/security/gate.py`) wraps the scanner and
is what every command calls. The two policies that matter:

- `BLOCK_POLICY` -- used by `install`, `compile`, and `unpack`.
  Critical findings block deployment. `--force` downgrades to a
  warning.
- `REPORT_POLICY` -- used by `apm audit` and the lockfile-driven
  `scan_lockfile_packages` (`src/apm_cli/security/file_scanner.py`).
  Collects findings without blocking; the command decides exit code.

`apm audit` exposes the scanner directly: `--file` for arbitrary files,
`--strip` (with optional `--dry-run`) to remove dangerous chars in
place, `-f sarif|json|markdown -o <path>` for CI reporting. Exit codes:
`0` clean, `1` critical, `2` warnings only. Source:
`src/apm_cli/commands/audit.py`.

## External scanner hardening

The experimental `external-scanners` feature can invoke a third-party SARIF
scanner (e.g. SkillSpector) and optionally run its LLM-powered analysis. That
adds a subprocess + network-egress surface, hardened as follows:

- **Allowlisted args only.** `--external-args` / `external.<name>.args` tokens
  are validated against a per-adapter allowlist of safe flag prefixes. Any
  non-allowlisted flag, secret-looking flag (`--token`, `--api-key`, ...), or
  path resolving outside the working directory is **rejected fail-closed** --
  the scan does not run. argv is always passed as a list (no `shell=True`).
- **Restrict-only policy.** A project `apm-policy.yml` can `allow_args: false`
  to strip args, but can never *add* argv tokens nor force LLM mode on. Only the
  local user opts into LLM egress (`--external-llm` / `external.<name>.llm`).
- **Credential hygiene.** LLM API keys (`OPENAI_API_KEY`,
  `NVIDIA_INFERENCE_KEY`) are forwarded to the scanner subprocess **only** when
  LLM mode is active for that run, and stripped otherwise. Scanner stderr is
  secret-redacted before it is surfaced in any error or log.
- **Project-vs-org trust boundary.** LLM mode sends scanned content to a
  third-party API, so it requires explicit user consent and is never triggered
  by an untrusted project-local policy file.

## Policy gates that block install

`apm-policy.yml` is evaluated **before** any download or write. The
preflight (`src/apm_cli/policy/install_preflight.py`) walks the
resolved dependency graph -- including transitive MCP servers -- and
fails the install if a dep is not in the allow list, falls under a
deny rule, uses a forbidden source/scope, or violates a configured
trust rule.

In CI, `apm audit --ci` runs the same baseline checks plus the policy
checks in `src/apm_cli/policy/policy_checks.py` (allow/deny lists,
target restrictions, MCP transport restrictions). Tighten-only
inheritance (enterprise -> org -> repo) is enforced in
`src/apm_cli/policy/inheritance.py`.

For schema and getting started, see [Get started with apm-policy.yml](./apm-policy-getting-started/) and
[Policy Reference](./policy-reference/).

## What APM does NOT do

State this plainly to your security reviewers:

- **Not a runtime sandbox.** APM exits after install. It does not
  monitor what your agent or any installed MCP server does with the
  context it received.
- **Not malware analysis.** APM does not statically analyse dep code or
  hooks for malicious behaviour. Hooks run in your harness, not in
  APM. Read them.
- **Not signing infrastructure.** APM has no concept of package
  signatures, key servers, or verified publishers today. Trust derives
  from the host (GitHub / ADO / GitLab), the pinned commit SHA, and
  the content hash -- not from a cryptographic identity attached to
  the package itself. (`src/apm_cli/install/cache_pin.py:24` notes
  signatures as deferred.) The SBOM export (above) is an inventory of
  what the lockfile recorded, not a signed attestation.
- **Not a license compliance tool.** APM records the *declared*
  license and emits it in the SBOM. It does not scan `LICENSE` text,
  conclude a license, check compatibility, or gate install on a
  license -- that is the compliance plane, downstream of APM.
- **Not transport security on `http://` deps.** See "Provenance"
  above.
- **Not protection against visible prompt injection.** The Unicode
  scanner catches *hidden* characters. A package author who writes a
  malicious instruction in plain English will pass every check.

## Recommended hardening

For an org standardising on APM:

- Require `GITHUB_APM_PAT` / `ADO_APM_PAT` from a secret store, never
  from developer dotfiles. Scope tokens to read-only on the source
  repos.
- Wire `apm audit --ci -f sarif -o audit.sarif` into branch protection
  and upload SARIF to GitHub code scanning.
- Publish an `apm-policy.yml` from your `<org>/.github` repo with an
  allow list and a transport restriction on MCP. See [Governance overview](./governance-overview/).
- Require signed commits on the source repos APM pulls from -- this is
  where APM's trust chain bottoms out.
- Route all dep traffic through an enterprise proxy with audit
  logging. See [Registry proxy](./registry-proxy/).
- Forbid `allow_insecure: true` in `apm.yml` via the policy allow
  list, except where an air-gapped mirror demands it.
