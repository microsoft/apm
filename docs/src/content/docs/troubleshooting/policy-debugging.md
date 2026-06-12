---
title: Debugging policy failures
description: Diagnose blocked installs and audits, inspect resolved org policy, and apply the right escape hatch.
sidebar:
  order: 5
---

When `apm install` or `apm audit --ci` blocks the project on policy, the
job is not to silence it -- it is to figure out **which rule fired,
where the policy came from, and whether the right fix is in your
project, in the org policy, or a one-shot escape hatch.**

This page walks the diagnosis end to end.

## 1. Read the failure

A policy block looks roughly like this:

```text
[x] policy: dependency.allow
    severity: error
    message:  Dependency 'acme-corp/internal-skills' is not on the
              org allowlist.
    evidence: apm.yml -> dependencies.apm[2]
    source:   acme-corp/.github/apm-policy.yml
```

Three things to extract before doing anything else:

- **Rule id** (`dependency.allow`) -- pin it; you will reuse it in the
  next steps.
- **Severity** (`error` blocks; `warning` does not).
- **Source** -- the org policy file that owns the rule. If you do not
  recognize it, jump straight to step 2.

## 2. Ask "which policy ruled on me?"

`apm policy status` prints the resolved policy posture for the current
project, with no side effects. Run it from the project root:

```bash
apm policy status
```

```text
APM Policy Status
-----------------
  Outcome        found
  Source         acme-corp/.github/apm-policy.yml
  Enforcement    enforce
  Cache age      4m ago
  Extends chain  acme-corp/.github/apm-policy.yml
  Effective rules  dependency: 2 rule(s); mcp: 3 rule(s); manifest: 1 rule(s)
```

Useful flags:

- `--no-cache` -- skip the policy cache and refetch from GitHub.
- `--policy-source <ref>` -- override discovery (e.g. `org`, a repo
  ref, or a local path) to test what *would* apply.
- `-o json` -- machine-readable for scripting and SIEM ingest.
- `--check` -- exit non-zero when no usable policy is resolved (CI
  pre-check). Default exit is always `0`.

If `Outcome` is anything other than `found`, the discovery itself
failed. Common causes: missing GitHub token, private `.github` repo,
network egress blocked. The `Notice:` line at the bottom of the report
explains which.

For the full command surface, see [`apm policy`](../reference/cli/policy/).

## 3. Inheritance and merge

Org policies can `extends:` other policies. The effective policy is the
**tighten-only merge** of the chain: a child can make rules stricter,
never looser.

```text
Extends chain  acme-corp/.github/apm-policy.yml
               -> acme-corp/policy-base@v3
```

Common surprises:

- A child adds an entry to `dependency.allow` but the parent's
  `dependency.deny` still applies -- denies always win.
- A child sets `mcp.transport.allow: [stdio, http]` but the parent
  pinned `[http]` -- the child's broader list is ignored.
- Severity can only be raised by a child, never lowered.

The full merge semantics live in
[Policy schema -> Merge rules (tighten-only)](../reference/policy-schema/#merge-rules-tighten-only).
Read that section before arguing with a parent policy.

## 4. Discovery surprises

APM resolves policy in this order:

1. `--policy-source <ref>` on `apm policy status`, or `--policy <ref>`
   on `apm audit --ci`. Explicit always wins.
2. Project-local `apm.yml` (`policy:` block, if present).
3. Auto-discovery: `<owner>/.github/apm-policy.yml` for the repo's
   GitHub owner.
4. None -- no enforcement.

Things that bite:

- **Wrong owner.** Auto-discovery uses the *current repo's* GitHub
  owner. If your fork is under a personal account, the org policy
  will not auto-apply. Use `apm policy status --policy-source org`
  to test against the upstream owner.
- **Stale cache.** Policy is cached on disk. If an admin just rolled
  out a fix, run with `--no-cache` (or `apm install --refresh ...`).
- **Discovery silently disabled.** `APM_POLICY_DISABLE=1` in the
  shell environment skips discovery for **every** APM command in that
  session. See the call-out below.

### Escape hatches

Two exist. Both are loud on purpose.

| Mechanism | Scope | Skips |
|---|---|---|
| `apm install --no-policy` | Single invocation | Org policy gate during install only. Does **not** bypass `apm audit --ci`. |
| `APM_POLICY_DISABLE=1` | Entire shell session | All policy discovery and enforcement, in every APM command. |

:::caution
`APM_POLICY_DISABLE=1` is session-wide and applies to install, audit,
compile, and unpack. Set it for one command (`APM_POLICY_DISABLE=1 apm
install`) -- never `export` it in a shell profile.
:::

Both flags are documented in
[Environment variables](../reference/environment-variables/).

## 5. Common rules that block

These cover most real-world failures. Each lists the symptom and the
shortest path to green.

### `dependency.allow` / `dependency.deny`

- **Symptom:** `Dependency '<owner>/<repo>' is not on the org
  allowlist` (or `is on the org denylist`).
- **Fix:** Replace the dep with an allowed equivalent, or open a PR
  against the org policy file to add it (see step 6).

### `mcp.transport.allow` excluding `stdio`

- **Symptom:** `MCP server '<name>' uses transport 'stdio' which is
  not on the allowed transport list`.
- **Cause:** Org has narrowed transports to remote-only (`http`,
  `sse`) and your `apm.yml` pins a stdio command.
- **Fix:** Re-install the server from the registry with `--url` (HTTP
  endpoint). If no remote endpoint exists, escalate to the org admin.

### `mcp.self_defined: deny`

- **Symptom:** `Self-defined MCP server '<name>' is not allowed; only
  registry-resolved servers are permitted`.
- **Cause:** Org requires every MCP server to come from the registry,
  not be hand-rolled in `apm.yml`.
- **Fix:** Find the server in the registry and `apm install
  mcp:<name>`. Remove the hand-rolled entry.

### `manifest.required_fields` missing

- **Symptom:** `Required manifest field 'description' is missing from
  apm.yml`.
- **Fix:** Edit `apm.yml` and add the listed field. Org policy
  defines the required set; check
  [Policy schema](../reference/policy-schema/).

### `unmanaged_files.action: deny`

- **Symptom:** `Unmanaged file detected in target directory:
  .github/instructions/extra.md`.
- **Cause:** A hand-edited file in a target directory APM owns. With
  `action: deny`, any file APM did not write blocks the build.
- **Fix:** Either move the file out of the target directory, or
  promote it to a real APM primitive in a package. See
  [Baseline checks](../reference/baseline-checks/) for the underlying
  CI check.

## 6. Working with org admins

Pick the right ask:

- **Fix the project** when: your dep is genuinely off-policy (a fork,
  unmaintained, replaced by an org-blessed alternative), or you can
  swap to a registry-listed MCP server.
- **Ask for an allowlist addition** when: the dep is critical, has no
  org-blessed equivalent, and meets the org's review bar.

When you open the PR against the org policy repo:

1. Link the failing CI run (or the `apm policy status` JSON output).
2. Quote the exact rule id and source line.
3. Link the org policy file you are amending so reviewers do not have
   to hunt for it.

For org-side context (who owns the policy, how rollouts work),
see [Governance overview](../enterprise/governance-overview/) and
[APM policy: getting started](../enterprise/apm-policy-getting-started/).

## See also

- [`apm policy`](../reference/cli/policy/) -- command reference.
- [Policy schema](../reference/policy-schema/) -- every rule, every
  field, merge semantics.
- [Baseline checks](../reference/baseline-checks/) -- the CI checks
  invoked by `apm audit --ci`.
- [Common errors](./common-errors/) -- non-policy install failures.
