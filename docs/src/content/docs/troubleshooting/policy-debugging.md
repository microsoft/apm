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

You usually meet a policy block in `apm install` first. It names the
policy source and prints one violation per offending dependency:

```text
[!] Policy: org:acme-corp/.github -- enforcement=block
[x] Policy violation: github.com/contoso/agent-skills -- not in allowed sources
  Blocked by org policy at org:acme-corp/.github (dependency-allowlist) -- remove `github.com/contoso/agent-skills` from apm.yml, contact admin to update policy, or use `--no-policy` for one-off bypass
[x] Install blocked by org policy -- see violations above
```

`apm install` exits `1` and writes nothing; a package you were adding is
removed from `apm.yml` again.

`apm audit --ci` reports the check that fired (here
`dependency-allowlist`) as a failed `[x]` row in its check table, then
lists the offending entries:

```text
  dependency-allowlist details:
    - github.com/contoso/agent-skills: not in allowed sources

[x] 1 of 11 check(s) failed
```

Three things to extract before doing anything else:

- **Check name** (`dependency-allowlist`, in parentheses in the
  `Blocked by` hint) -- pin it; you will reuse it in the next steps.
- **Enforcement** -- `enforcement=block` fails the command. Under `warn`,
  `apm install` continues and lists the finding under `policy warning`
  in its summary.
- **Source** -- the policy that ruled (`org:<org>/.github` when
  auto-discovered from your git remote). Only `apm install` prints it; if
  you do not recognize it, or you are reading audit output, jump
  straight to step 2.

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
  Enforcement    block
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

For the full command surface, see [`apm policy`](../../reference/cli/policy/).

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
- `enforcement` can only be raised by a child (`off` -> `warn` ->
  `block`), never lowered.

The full merge semantics live in
[Policy schema -> Merge rules (tighten-only)](../../reference/policy-schema/#merge-rules).
Read that section before arguing with a parent policy.

## 4. Discovery surprises

APM resolves policy in this order:

1. `--policy-source <ref>` on `apm policy status`, or `--policy <ref>`
   on `apm audit --ci`. Explicit always wins.
2. Auto-discovery from the git remote: `apm-policy.yml` in the owner's
   `.github-private`, `.github`, `.apm`, then `_apm` repo.
3. None -- no enforcement. (`policy:` in `apm.yml` only pins `hash`
   and `fetch_failure_default`; it does not choose the source.)

Things that bite:

- **Wrong owner.** Auto-discovery uses the *current repo's* GitHub
  owner. If your fork is under a personal account, the org policy
  will not auto-apply. Use `apm policy status --policy-source
  <upstream-owner>/.github` to test against the upstream owner.
- **Stale cache.** Policy is cached on disk. If an admin just rolled
  out a fix, run `apm policy status --no-cache` (or
  `apm audit --ci --no-cache`) to refetch and refresh the cache.
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
[Environment variables](../../reference/environment-variables/).

## 5. Common rules that block

These cover most real-world failures. Each lists the symptom and the
shortest path to green.

### `dependency-allowlist` / `dependency-denylist`

- **Symptom:** `Policy violation: <owner>/<repo> -- not in allowed
  sources` (or `denied by pattern: <pattern>`).
- **Fix:** Replace the dep with an allowed equivalent, or open a PR
  against the org policy file to add it (see step 6).

### `mcp-transport` excluding `stdio`

- **Symptom:** `<name>: transport 'stdio' not in allowed [http, sse]`.
- **Cause:** Org has narrowed transports to remote-only (`http`,
  `sse`) and your `apm.yml` pins a stdio command.
- **Fix:** Re-install the server from the registry with `--url` (HTTP
  endpoint). If no remote endpoint exists, escalate to the org admin.

### `mcp-self-defined`

- **Symptom:** `N self-defined MCP server(s) denied by policy`, with
  `<name>: self-defined server` in the details.
- **Cause:** Org requires every MCP server to come from the registry,
  not be hand-rolled in `apm.yml`.
- **Fix:** Find the server in the registry and `apm install
  --mcp <name>`. Remove the hand-rolled entry.

### `required-manifest-fields`

- **Symptom:** `1 required manifest field(s) missing`, with the field
  name (for example `description`) in the details.
- **Fix:** Edit `apm.yml` and add the listed field. Org policy
  defines the required set; check
  [Policy schema](../../reference/policy-schema/).

### `unmanaged-files`

- **Symptom:** `N unmanaged file(s) in governance directories`, with
  each path (for example `.github/instructions/extra.md`) in the details.
- **Cause:** A hand-edited file in a target directory APM owns. With
  `action: deny`, any file APM did not write blocks the build.
- **Fix:** Either move the file out of the target directory, or
  promote it to a real APM primitive in a package. See
  [Baseline checks](../../reference/baseline-checks/) for the underlying
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
2. Quote the exact check name and violation line.
3. Link the org policy file you are amending so reviewers do not have
   to hunt for it.

For org-side context (who owns the policy, how rollouts work),
see [Governance deep-dive](../../enterprise/governance-guide/) and
[APM policy: getting started](../../enterprise/apm-policy/).

## See also

- [`apm policy`](../../reference/cli/policy/) -- command reference.
- [Policy schema](../../reference/policy-schema/) -- every rule, every
  field, merge semantics.
- [Baseline checks](../../reference/baseline-checks/) -- the CI checks
  invoked by `apm audit --ci`.
- [Common errors](../common-errors/) -- non-policy install failures.
