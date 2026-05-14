---
title: "Wire APM Into GitHub Rulesets"
description: "Ops-side recipe for requiring the apm-audit check and lockfile consistency on protected branches via org-level GitHub Repository Rulesets."
sidebar:
  order: 9
---

This is the GitHub-admin recipe. APM ships the CI job; GitHub
Repository Rulesets convert "we have a green check" into "you cannot
merge without it". Nothing on this page is APM functionality -- it is
configuration on the GitHub side that pairs with the workflow from
[Enforce in CI](../enforce-in-ci/).

## Why rulesets, not classic branch protection

Classic branch protection rules are per-repo and click-driven. Rulesets
are GitHub's current recommendation because they:

- Apply at the **organization** level and target many repos at once
  (by name pattern, by topic, by repo property).
- Are **versioned and exportable** -- the JSON definition lives next
  to your IaC, not in 200 repo settings pages.
- Support **layered evaluation** -- a repo can be covered by an org
  ruleset and a repo ruleset; the strictest setting wins.
- Have an **explicit bypass-actor list** with audit log entries when
  bypass is used.

For an org-wide governance program, the per-repo branch-protection
clickwheel does not scale. Use rulesets.

## The check name APM publishes

The recommended workflow in [Enforce in CI](../enforce-in-ci/) emits a
single status check whose name is the **job name** in the workflow
file. The template uses:

```yaml
# .github/workflows/apm-audit.yml
name: APM Audit
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: microsoft/apm-action@v1
        with:
          audit-report: true
        env:
          GITHUB_APM_PAT: ${{ secrets.APM_PAT }}
```

GitHub registers the check as `audit` (the job key). When multiple
workflows publish a job called `audit`, GitHub disambiguates as
`APM Audit / audit`. **Run the workflow at least once on any repo
before adding it to a ruleset** -- GitHub will not autocomplete a
check name it has never seen.

If your org standardises on a different job name, use that name
verbatim in the ruleset. The string must match exactly.

## Recipe 1: org ruleset requiring apm-audit on default branch

Goal: every repo in the org with an `apm.yml` cannot merge to its
default branch unless the `audit` check passes.

1. In your org, go to **Settings** > **Repository** > **Rulesets** >
   **New ruleset** > **New branch ruleset**.
2. **Name**: `apm-audit-required`.
3. **Enforcement status**: `Active`. Use `Evaluate` first if you want
   a dry-run period -- it logs would-be violations without blocking.
4. **Bypass list**: leave empty for true enforcement. See pitfalls
   below before adding any actor.
5. **Target repositories**: dynamic targeting works best.
   - `All repositories`, or
   - `Repositories matching properties` and target a custom property
     like `uses-apm = true` (set this property on repos that have an
     `apm.yml`).
6. **Target branches**: `Include default branch`.
7. **Rules**: enable **Require status checks to pass**.
   - Add status check: `audit` (or `APM Audit / audit` if you have
     name collisions).
   - Source: `GitHub Actions`.
   - Enable **Require branches to be up to date before merging** so
     stale PRs cannot bypass a freshly-tightened policy.
8. Save.

PRs in matching repos now show `audit` as a required check. A red
audit blocks merge regardless of repo-admin settings.

## Recipe 2: ensure the lockfile is checked in

The honest answer: **GitHub Rulesets do not have a native "if file A
changed, file B must also change" rule.** The check is enforced by
APM itself, not by the ruleset.

`apm audit --ci` runs lockfile consistency checks unconditionally
(see [Governance overview](../governance-overview/) for the non-bypass contract).
A PR that edits `apm.yml` without updating `apm.lock.yaml` -- or
commits a lockfile that does not match the manifest -- fails the
`audit` check. So Recipe 1 already covers this case: if `audit` is
required, lockfile drift cannot merge.

What rulesets *can* add on top:

- **Restrict file paths** rule: block direct edits to `apm.lock.yaml`
  outside of PRs. Combined with CODEOWNERS on the lockfile, this
  forces lockfile changes through review.
- **Require a pull request before merging**: prevents `git push` of
  manifest changes straight to default.

Together: Recipe 1 catches the *content* drift, the file-path rule
catches the *workflow* bypass.

## Recipe 3 (optional): signed commits on producer repos

For repos that publish APM packages (the producer side), turn on
**Require signed commits** in the same ruleset or a separate one
scoped to producer repos. This pairs with `apm pack` content hashing:
the lockfile pins what was published, and signed commits give you a
verifiable author chain on the source side.

This is most valuable on the `<org>/.github` repo that hosts
`apm-policy.yml` -- see [Governance overview](../governance-overview/) for the
trust-anchor rationale.

## Pitfalls

- **Bypass actors**: every actor on the bypass list can merge a red
  `audit`. The action is logged, but it merges. Keep the list empty,
  or restrict it to a break-glass team that is on-call rotation only.
  "Org admins" as a default bypass defeats the gate.
- **Check name typos**: the required-check string is matched
  literally. `Audit`, `audit `, or `APM Audit` (workflow name, not
  job name) silently never match -- the check appears as
  `Expected -- Waiting for status` and the PR is mergeable in some
  configurations. Always run the workflow once and copy the
  registered name from the PR's checks tab.
- **`Evaluate` left on forever**: dry-run mode logs but does not
  block. Set a calendar reminder to flip to `Active` within two
  weeks of pilot.
- **Repo-level rulesets overriding org rules**: rulesets layer
  additively for *requirements*, but a repo admin cannot weaken an
  org-required check -- they can only add more. If a repo seems to
  be merging without `audit`, check whether the org ruleset's repo
  targeting actually includes that repo.

## Related

- [Enforce in CI](../enforce-in-ci/) -- the workflow that emits the
  check this page requires.
- [Governance overview](../governance-overview/) -- bypass contract, install-gate
  guarantees, what `apm audit --ci` actually verifies.
- [APM policy reference](../policy-reference/) -- the policy schema
  the audit check evaluates.
