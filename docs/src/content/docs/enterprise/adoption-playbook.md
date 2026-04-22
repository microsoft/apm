---
title: "Adoption Playbook"
description: "A phased guide to rolling out APM from a pilot team to organization-wide adoption."
sidebar:
  order: 7
---

APM adoption follows a proven pattern: start small, prove value, expand.
This playbook walks platform teams through each phase with concrete
milestones, success metrics, and rollback options so you can move quickly
without betting the farm.

## Before You Begin

Confirm these prerequisites before kicking off Phase 1:

- APM is [installed](../../getting-started/installation/) and available in
  your terminal.
- You have identified a pilot team willing to try a new workflow for two
  weeks.
- You have a Git-hosted repository where the pilot team can work.
- You have read access to at least one APM package registry (public or
  private).

## Phase 1 -- Pilot (Week 1-2)

**Goal:** One team, one project, one command to a working environment.

### Steps

1. Choose a single project and a pilot team of 3-5 engineers.
2. Pick 2-3 APM packages that cover the team's most common configuration
   (for example, a linter ruleset, a set of agent instructions, and a
   shared prompt library).
3. Run `apm init` in the project root to scaffold `apm.yml`.
4. Add the selected packages as dependencies:

   ```bash
   apm add org/lint-standards org/agent-instructions org/prompt-library
   ```

5. Run `apm install` to deploy files.
6. Commit `apm.yml` and `apm.lock.yaml` to the repository.

### Verification

Every team member should be able to run:

```bash
git clone <repo> && cd <repo> && apm install
```

and arrive at an identical, ready-to-work environment with no additional
manual steps.

### Success Metric

Onboarding time drops from "read the README and manually copy files" to a
single command.

### What to Watch

- Installation friction (missing runtimes, network issues).
- Unexpected file placement -- review `apm.lock.yaml` to confirm paths.
- Authentication errors when pulling private packages.

---

## Phase 2 -- Shared Package (Week 3-4)

**Goal:** Centralize standards in a reusable package that the pilot team
consumes.

### Steps

1. Create your first organization package (for example,
   `myorg/apm-standards`).
2. Include baseline content:
   - Coding standards instructions for agents.
   - Security baseline configurations.
   - Common prompts the team uses daily.
3. Publish the package to your registry.
4. Add it to the pilot project:

   ```bash
   apm add myorg/apm-standards
   apm install
   ```

5. Verify that the pilot team receives the new files on their next
   `apm install`.

### Success Metric

When you update the shared package and the pilot team runs
`apm deps update`, the latest standards land in their project
automatically.

### What to Watch

- Version pinning: confirm that `apm.lock.yaml` captures the exact version
  installed.
- File collisions: if the shared package deploys a file that already exists,
  decide whether to force-overwrite or skip.

---

## Phase 3 -- CI Integration (Month 2)

**Goal:** Enforce content safety in the pipeline so compromised packages
cannot reach production.

### Steps

1. Add APM to your CI pipeline. `apm install` blocks deployment if any
   package contains critical hidden-character findings — no additional
   configuration needed:

   ```yaml
   - uses: microsoft/apm-action@v1
     with:
       audit-report: true   # Generate SARIF report for Code Scanning
   ```

   For SARIF upload to GitHub Code Scanning, add:

   ```yaml
   - uses: github/codeql-action/upload-sarif@v3
     if: always() && steps.apm.outputs.audit-report-path
     with:
       sarif_file: ${{ steps.apm.outputs.audit-report-path }}
       category: apm-audit
   ```

2. Ensure `apm.lock.yaml` is committed so installs are reproducible.

### Success Metric

Pull requests are blocked when packages contain critical hidden-character
findings. No unsafe content reaches the default branch.

### What to Watch

- Build time impact. APM operations are fast, but confirm they add
  acceptable overhead.
- Lock file conflicts when multiple PRs update dependencies concurrently.
  Resolve the same way you handle lock file conflicts in npm or pip.

---

## Phase 4 -- Second Team (Month 2-3)

**Goal:** Validate that the pattern transfers to a different team and
project.

### Steps

1. Onboard a second team using the same shared package from Phase 2.
2. The second team runs `apm init`, adds the shared package, and runs
   `apm install` -- the same workflow the pilot team followed.
3. Gather structured feedback:
   - Did the shared package cover their needs, or are additions required?
   - Were there file conflicts specific to their project layout?
   - How long did onboarding take compared to their previous process?
4. Iterate on the shared package based on feedback.

### Success Metric

A different project, with a different codebase, arrives at the same
standards and the same workflow through the same shared package.

### What to Watch

- Edge cases in project structure that the shared package did not
  anticipate.
- Requests for team-specific overrides. APM supports layered
  configuration, so teams can extend the shared package without forking it.

---

## Phase 5 -- Org-Wide Rollout (Month 3+)

**Goal:** Establish APM as the standard mechanism for managing agent and
tool configuration across the organization.

### Steps

1. Document the pattern in an internal guide. Include:
   - How to add APM to an existing project.
   - How to create and publish shared packages.
   - How to handle common issues (file conflicts, version pinning,
     registry authentication).
2. Mandate `apm.yml` for new projects. For existing projects, adoption can
   be voluntary initially.
3. Enable content scanning across repositories using CI audit steps.
4. Assign package ownership. Each shared package should have a
   maintainer or a maintaining team.

### Success Metric

80% or more of active repositories contain an `apm.yml` and pass
`apm install` content scanning in CI.

### What to Watch

- Stale packages. Set a review cadence for shared packages.
- Permission sprawl. Limit who can publish packages to the organization
  registry.
- Adoption gaps. Track which teams have not yet onboarded and offer
  hands-on support.

---

## Common Objections

Adoption conversations surface the same questions repeatedly. Here are
direct answers.

### "We already have tool plugins configured."

APM does not replace your existing configuration. It wraps and manages the
files your tools already read. You gain a lock file, version pinning, and
cross-project consistency on top of what you already have.

### "This is another tool to maintain."

APM has zero runtime footprint. It generates files and exits. There is no
daemon, no background process, and no runtime dependency in your
application. Maintenance cost is limited to updating package versions in
`apm.yml`.

### "What if we stop using it?"

Delete `apm.yml` and `apm.lock.yaml`. The native configuration files APM
deployed remain in place and continue to work exactly as they did before.
There is no lock-in.

### "Our developers will not adopt this."

One command replaces multiple manual setup steps. Teams that adopt APM
report that the workflow is self-reinforcing: once a developer sees
`apm install` reproduce a working environment in seconds, they do not
go back to manual configuration.

---

## Rollback Plan

At any phase, you can reverse course:

1. Remove `apm.yml` and `apm.lock.yaml` from the repository.
2. The configuration files APM deployed remain on disk and continue to
   function. Your tools read native files, not APM-specific formats.
3. Optionally, remove APM from CI steps.

APM is designed for zero lock-in. Removing it leaves your project in a
working state with standard configuration files.

---

## Related Resources

- [Getting Started](../../getting-started/installation/) -- Install APM
  and create your first project.
- [Org-Wide Packages](../../guides/org-packages/) -- Create and manage
  shared packages for your organization.
- [CI/CD Pipelines](../../integrations/ci-cd/) -- Add APM
  to your continuous integration pipeline.
- [Governance](../governance/) -- Enforce standards and
  audit compliance across repositories.
