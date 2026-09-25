---
title: Source packages and installed copies
description: Understand the source-of-truth repository and the materialized copy APM installs for consumers.
---

An APM package can appear in two places on the same machine without creating
two sources of truth. They serve different roles:

- **The package repository is canonical.** Authors edit primitives in the Git
  repository where the package is maintained.
- **The installed copy is materialized consumer content.** `apm install`
  resolves the declared dependency and stages the pinned package under
  `apm_modules/` before integrating its primitives into the selected targets.

Treat the installed copy the same way you would treat `node_modules/`: do not
edit it and do not commit it as the authoritative package source.

## Recommended author/consumer workflow

Keep package development and package consumption explicit:

1. Make primitive changes in the package's source Git repository.
2. In the consumer project, declare that repository as a `git:` dependency in
   `apm.yml`, with the `ref:` you want the project to consume.
3. Run `apm install` in the consumer project. APM materializes the dependency
   and records the resolved revision in `apm.lock.yaml`.
4. When the source package changes, update the declared `ref:` (or use the
   appropriate update workflow) and install again.

For example:

```yaml
dependencies:
  - git: acme/ai-primitives
    ref: v1.4.0
```

This separation makes it clear which files are authored and which files are
generated from a pinned dependency.

## When authoring and consuming in the same repository

If you are actively editing a package in its own repository, work on the source
files directly. Installing that same package globally or back into the same
repository creates a second consumer view that is useful for testing, but it is
not the place to make edits.

For integration testing, a separate consumer fixture or sample project keeps
the boundary clearest: point it at the source repository/ref, install, and
verify the generated target files there.

APM does not currently provide a live-link/development-install mode that keeps
an installed consumer copy synchronized with edits in the source checkout. If
you need immediate feedback while authoring, test from the source repository or
re-run the install/update workflow after changes.
