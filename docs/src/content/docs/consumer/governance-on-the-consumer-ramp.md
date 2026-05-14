---
title: Governance on the consumer ramp
description: What apm-policy.yml means for your install, how to see which policy is in effect, and what to do when a policy blocks you.
---

If your org has set up an `apm-policy.yml`, every `apm install` you run
is checked against it before anything is written to disk. You did not
write this file. Your platform or security team did. This page covers
what that means for your day-to-day work.

## Where the policy lives

APM auto-discovers `apm-policy.yml` from your project's git remote. For
a repo on `github.com/your-org/your-repo`, APM looks in
`your-org/.github/apm-policy.yml`. The file is fetched, cached, and
re-checked on every install.

You do not need to install or configure anything. If the file exists
in your org's `.github` repository, every `apm install` run from a repo
in that org is governed by it.

## What the policy can do to your install

The schema is defined in `src/apm_cli/policy/schema.py`. The fields
your platform team can set, and what each one does to your install:

- **`enforcement`** -- `warn`, `block`, or `off`. `block` aborts the
  install on any violation; `warn` lets it proceed but logs each
  violation; `off` disables enforcement.
- **`dependencies.allow`** / **`dependencies.deny`** -- glob patterns
  over package refs. A package matching `deny` (or not matching `allow`,
  when `allow` is set) is rejected.
- **`dependencies.require`** -- packages your `apm.yml` must include.
- **`dependencies.max_depth`** -- maximum transitive dependency depth.
- **`mcp.allow`** / **`mcp.deny`** -- glob patterns over MCP server
  references. Same semantics as the dependency lists.
- **`mcp.transport.allow`** -- restricts MCP transports
  (`stdio`, `sse`, `http`, `streamable-http`).
- **`mcp.self_defined`** -- `allow`, `warn`, or `deny` for MCP servers
  declared inline in your `apm.yml` rather than pulled from a package.
- **`mcp.trust_transitive`** -- whether MCP servers shipped by deep
  dependencies are trusted automatically.
- **`compilation.target.allow`** -- which harness targets your repo can
  compile to (`vscode`, `claude`, `all`).
- **`compilation.strategy.enforce`** -- `distributed` or `single-file`.
- **`manifest.required_fields`** / **`manifest.scripts`** /
  **`manifest.require_explicit_includes`** -- shape constraints on
  your `apm.yml` itself.
- **`unmanaged_files.action`** -- `ignore`, `warn`, or `deny` for files
  in agent directories that the lockfile does not track.
- **`extends`** -- the policy may inherit from another policy
  (enterprise -> org -> repo). Inheritance only tightens; it cannot
  loosen a parent's rules.

## See which policy is in effect

```bash
apm policy status
```

Prints the active policy: where it was discovered, the enforcement
level, the cache age, the `extends` chain, and a count of effective
rules per section.

```
APM Policy Status
  Outcome          found
  Source           org:your-org/.github
  Enforcement      block
  Cache age        12m ago
  Extends chain    enterprise/policies
  Effective rules  3 dependency denies; 2 mcp transport restrictions
```

Useful flags:

- `--no-cache` forces a fresh fetch.
- `-o json` emits the same report as JSON for scripting.
- `--policy-source <ref>` overrides discovery (e.g. point at a local
  file you are testing).
- `--check` exits non-zero when no policy is found, for CI pre-checks.

For a deeper look at which packages would pass or fail, run:

```bash
apm audit --ci --policy org
```

This runs the full set of policy checks and reports every violation.

## When your install is blocked

A blocked install ends with output like:

```
[x] Install blocked by org policy -- see violations above
```

Each individual violation is rendered with its remediation hint:

```
Blocked by org policy at org:your-org/.github
  -- remove `untrusted-org/some-pkg` from apm.yml,
     contact admin to update policy,
     or use `--no-policy` for one-off bypass
```

You have three options:

1. **Pick a different package.** If `untrusted-org/some-pkg` is denied,
   find an allowed alternative and update your `apm.yml`.
2. **Ask your platform team to update the policy.** If you have a
   legitimate reason to use the package, the policy owner can add it
   to the `allow` list or remove it from `deny`.
3. **Bypass for a single run with `--no-policy`.** This skips org
   policy enforcement for that one invocation. It does not bypass
   `apm audit --ci`, so any CI gate still catches it. Treat this as a
   diagnostic switch, not a workflow.

## Preview a policy decision before running install

```bash
apm install --dry-run
```

Runs discovery and policy checks without writing to disk. Each
violation is reported as `Would be blocked by policy: <dep> -- <reason>`
or `Policy warning: <dep> -- <reason>`. Useful before you commit a
manifest change you suspect the policy will reject.

## Disabling discovery

Two escape hatches exist:

- `--no-policy` on a single `apm install` or `apm audit` invocation.
- `APM_POLICY_DISABLE=1` as an environment variable, for the same
  scope as `--no-policy`.

Neither hides anything from `apm audit --ci` running in CI. They only
relax local enforcement.

If you are the one writing the policy, see [Governance overview](../../enterprise/governance-overview/)
for the platform-team view -- this page is the consumer's view of a policy
that is already in place.
