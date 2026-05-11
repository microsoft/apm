---
title: Get Started with apm-policy.yml
description: Stand up a working org policy that blocks one bad case in 20 minutes.
sidebar:
  order: 2
---

`apm-policy.yml` is one YAML file that gates what your repos may install.
This page takes you from nothing to a working policy that blocks a
specific package, in 20 minutes.

## 1. Where the file lives

APM auto-discovers a policy from the project's git remote. For a repo
on `github.com/<org>/<project>`, APM fetches:

```
<org>/.github/apm-policy.yml
```

Push this one file to your org's `.github` repo and every repo in the
org is governed by it on the next `apm install`. No per-repo wiring,
no settings page.

You can also point at a policy explicitly during testing -- before
the file is committed to your `.github` repo:

```bash
# Preview what the policy would do, against any source:
apm policy status --policy-source ./apm-policy.yml
apm policy status --policy-source https://example.com/apm-policy.yml
apm policy status --policy-source contoso/governance     # any owner/repo

# Gate CI on a policy without auto-discovery:
apm audit --ci --policy ./apm-policy.yml
```

(`apm install` does not take a `--policy` flag -- discovery on install
runs automatically from your git remote, or you can force-skip it
with `--no-policy` / `APM_POLICY_DISABLE=1`.)

The result is cached locally for 1 hour by default; `--no-cache` forces
a fresh fetch.

## 2. The minimal working policy

Copy this into `<your-org>/.github/apm-policy.yml`. It blocks one
package family and starts in `warn` so you do not break anyone on day
one:

```yaml
name: "Acme baseline policy"
version: "1.0.0"
enforcement: warn        # warn | block | off

dependencies:
  deny:
    - "untrusted-org/**"
```

Commit, push. On the next `apm install` in any repo in the org, a user
who depends on `untrusted-org/anything` will see a warning. When you
are ready to enforce, flip `enforcement: block`. See
[../policy-pilot/](../policy-pilot/) for the rollout pattern.

## 3. The schema in one screen

Every top-level key, and what it does. Field names come straight from
the schema -- do not invent others.

```yaml
name: ""                  # display name
version: ""               # your policy version
extends: null             # "org" | "<owner>/<repo>" | "https://..."
enforcement: warn         # warn | block | off
fetch_failure: warn       # warn | block (when policy can't be fetched)
cache:
  ttl: 3600               # seconds

dependencies:
  allow: null             # null = no opinion; [] = nothing allowed
  deny: []
  require: []             # packages every repo must include
  require_resolution: project-wins   # project-wins | policy-wins | block
  max_depth: 50

mcp:
  allow: null
  deny: []
  transport:
    allow: null           # stdio | sse | http | streamable-http
  self_defined: warn      # allow | warn | deny (inline MCPs in apm.yml)
  trust_transitive: false

compilation:
  target:
    allow: null           # vscode | claude | all
    enforce: null
  strategy:
    enforce: null         # distributed | single-file
  source_attribution: false

manifest:
  required_fields: []
  scripts: allow          # allow | deny
  content_types: null     # {allow: [...]}
  require_explicit_includes: false

unmanaged_files:
  action: ignore          # ignore | warn | deny
  directories: []
```

Allow-list semantics: `null` means "no opinion", an empty list means
"explicitly allow nothing", a populated list means "only these".
Deny and require lists accumulate. For per-field detail, see
[../policy-reference/](../policy-reference/).

## 4. Inheritance: enterprise, org, repo

A policy may inherit from another via `extends`. The chain is resolved
left-to-right, max depth 5, and **child can only tighten parent** --
never relax it.

```yaml
# in <enterprise-org>/.github/apm-policy.yml -- the hub
name: "Acme enterprise baseline"
enforcement: block
dependencies:
  deny: ["malware-org/**"]
```

```yaml
# in <team-org>/.github/apm-policy.yml -- inherits + adds
name: "Payments team policy"
extends: "acme-enterprise/.github"
dependencies:
  deny: ["legacy-internal/**"]   # union'd with parent
mcp:
  transport:
    allow: [stdio]               # tightens transport
```

Merge rules: deny/require lists union, allow-lists intersect (tighten),
enforcement escalates to the strictest level on the ladder
(`off` < `warn` < `block`). A repo-level child cannot downgrade a
parent that says `block`.

## 5. Where the policy runs

Discovery and enforcement run as a preflight on every install path:
`apm install`, `apm install <pkg>`, `apm install --mcp`,
`apm install --dry-run`. The same checks run during `apm compile` and
`apm audit`. See [../../concepts/lifecycle/](../../concepts/lifecycle/)
for where the gate sits in the install pipeline.

For CI gating that runs even when a developer passes `--no-policy`
locally, wire `apm audit --ci --policy org` into your pipeline. See
[../enforce-in-ci/](../enforce-in-ci/).

## 6. What a blocked install looks like

When `enforcement: block` and a check fails, the user sees an inline
error per violation followed by an abort:

```
[x] Policy violation: untrusted-org/some-pkg -- denied by policy rule
      -- remove `untrusted-org/some-pkg` from apm.yml, contact admin to update policy, or use `--no-policy` for one-off bypass
[x] Install blocked by org policy -- see violations above
```

In `warn` mode the same lines render as warnings and the install
proceeds. To preview without writing anything, run `apm install
--dry-run`; each blocked dep prints as
`Would be blocked by policy: <dep> -- <reason>`. The consumer-side
view of all this lives in
[../../consumer/governance-on-the-consumer-ramp/](../../consumer/governance-on-the-consumer-ramp/).

## Next

You have a policy that warns on one bad case. To roll it out without
breaking your org, follow [../policy-pilot/](../policy-pilot/) -- pilot
in `warn`, observe, then flip to `block`.
