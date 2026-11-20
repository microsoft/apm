---
title: Test a package locally
description: Install your working copy into a consumer repository, swap it in across a dependency graph, and turn consumer edits back into patches before you tag.
---

Use this page before you tag a release. You will install the package from your
disk into a consumer repository, see your changes land on every `apm install`,
and check the result the way a consumer would. The examples continue from
[Create a package](../create-a-package/): your package is `../my-skills`, next to
a scratch consumer repository.

## Install your working copy

In a scratch consumer repository with an `apm.yml`, install the package by path:

```bash frame="terminal"
apm install ../my-skills
```

```text frame="none"
[+] ../my-skills
[*] Updated apm.yml with 1 new package(s)
...
[i] Targets: claude, copilot  (source: apm.yml)
  [+] ../my-skills (local)
  |-- 1 skill(s) integrated -> .agents/skills/, .claude/skills/
...
[*] Installed 1 APM dependency in 0.4s.
```

APM copies the folder into `apm_modules/_local/my-skills/` and deploys it like
any other package. Edit a file in `../my-skills`, run `apm install` in the
consumer again, and the deployed copy changes. You do not need to commit or tag
in between.

The consumer's lockfile records `source: local` and the path. Keep this in a
scratch repository: a teammate's clone has no `../my-skills`. Once you tag,
replace the path with `you/my-skills#v1.1.0`.

## Swap your copy in across the graph

Sometimes your package reaches the consumer through another package, so there
is no line to change. A `replace:` rule in the consumer's root `apm.yml`
substitutes your local checkout, or a fork at a ref, everywhere that package
appears:

```yaml title="apm.yml"
$schema: https://microsoft.github.io/apm/specs/schemas/manifest-source-replacements-v1.schema.json
dependencies:
  apm:
    - contoso/review-skills#v1.0.0
replace:
  - for:
      git: https://github.com/contoso/review-skills.git
    with:
      path: ../my-skills
```

`apm install` prints `[i] Replacement: contoso/review-skills#v1.0.0 -> ../my-skills`
and deploys your copy in place of the tag. For a fork, write
`with: {git: https://github.com/you/review-skills-fork.git, ref: my-fix}`.
Rules apply to direct and transitive dependencies. They belong in the
consumer's root `apm.yml`; rules declared inside a dependency do not
redirect the consumer.

:::caution[Candidate feature]
`replace:` is an unratified OpenAPM candidate. It needs the `$schema` line above,
and some older APM versions ignore the rule and install the original package.
Keep replacement manifests in local or scratch work; do not share them with
teammates on other APM versions. Details:
[Manifest schema](../../reference/manifest-schema/).
:::

## Keep maintainer dependencies out of your release

A package can depend on things its maintainers need and consumers do not, such as a test
fixture package or a release checklist skill. Declare them as development
dependencies:

```bash frame="terminal"
apm install --dev owner/test-helpers
```

```diff title="apm.yml"
+devDependencies:
+  apm:
+    - owner/test-helpers
```

`apm install` in your package repository deploys both `dependencies` and
`devDependencies`. A consumer that installs your package resolves your `dependencies`
and skips `devDependencies`, and `apm pack` leaves development dependencies out of plugin
bundles.

## Turn consumer edits into patches

A consumer who fixes a deployed file by hand can send you the fix. In their
repository:

```bash frame="terminal"
apm export-patch
```

```text frame="none"
[>] Replaying install (cache-only)...
[+] Replayed 2 package(s)
[i] .claude/skills/style-checker/SKILL.md -> microsoft/apm-sample-package:.apm/skills/style-checker/SKILL.md
[i] wrote /work/app/patches/microsoft__apm-sample-package@v1.0.0.patch
[*] Exported 1 edit(s) into 1 patch file(s) under /work/app/patches. Apply each with 'git apply' from the package repository root at the base noted in the patch header.
```

The patch maps the edited deployed file back to its source in your package:

```diff title="patches/microsoft__apm-sample-package@v1.0.0.patch"
# Exported by 'apm export-patch'
# package: microsoft/apm-sample-package
# source: microsoft/apm-sample-package
# base: commit fb2851683be0e0e7711421d518bd8dba23b0b1f6 (v1.0.0)
# Apply from the package repository root, checked out at the
# base above:  git apply <this file>
--- a/.apm/skills/style-checker/SKILL.md
+++ b/.apm/skills/style-checker/SKILL.md
@@ -15,3 +15,4 @@
 - Checks spacing values against the spacing scale
 - Ensures typography follows the defined type scale
 - Flags inconsistent naming conventions in CSS classes
+- Prefer small components.
```

Check out the base commit in your package repository and run
`git apply <file>`. `apm export-patch` skips local-path packages: their source
is already on disk.

## Check before you tag

In your package repository, run the same check consumers run:

```bash frame="terminal"
apm install
apm audit
```

`apm audit` compares deployed files with the lockfile and scans them for hidden
Unicode characters. To scan one file you are still writing, use
`apm audit --file .apm/skills/review/SKILL.md`. Fix any finding before the
tag: a consumer's install blocks a critical finding in your package.

## Verify

- The consumer's deployed files match your latest edit after `apm install`.
- `apm audit` in your package repository prints `No drift detected` and no
  findings.
- Every relative link in a deployed agent, instruction or prompt opens the file
  you meant.

## Related

- [Release a version](../release-a-version/): tag what you tested.
- [`apm export-patch`](../../reference/cli/export-patch/): every option.
- [`apm audit`](../../reference/cli/audit/): formats and CI use.
