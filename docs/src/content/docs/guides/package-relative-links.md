---
title: "Package-Relative Links"
description: "How APM handles relative markdown links between primitives and assets inside a package."
sidebar:
  order: 10
---

A primitive in your package can link to a sibling asset (another markdown
file, a guide, a JSON example) using a normal relative markdown link.
APM keeps those links working after install -- even though primitives are
deployed to host-tool-specific locations (`.github/instructions/`,
`.agents/skills/`, `.cursor/rules/`, ...) that no longer match the
package's authoring layout.

## The pattern

Author your package with relative links as if the layout were preserved:

```
your-package/
+-- apm.yml
+-- .apm/
|   +-- instructions/
|       +-- python-style.instructions.md
+-- standards/
    +-- pep8.md
```

```markdown
<!-- .apm/instructions/python-style.instructions.md -->
---
applyTo: "**/*.py"
---

# Python style

Follow the [PEP8 reference](../../standards/pep8.md).
```

After `apm install` in a consumer:

```
consumer/
+-- .github/instructions/
|   +-- python-style.instructions.md   # link rewritten
+-- apm_modules/
    +-- <owner>/<package>/             # full package preserved
        +-- standards/pep8.md
```

The deployed instruction body now contains a path that points at the
package's install location:

```markdown
Follow the [PEP8 reference](../../apm_modules/<owner>/<package>/standards/pep8.md).
```

The link resolves on disk and the host tool can follow it.

## What gets rewritten

APM rewrites a markdown link at install time when **all** of the
following hold:

- The link is relative (no `http:`, `mailto:`, scheme, or leading `/`).
- The resolved target stays inside the source package's root.
- The target file exists in the package.

Links that fail any of those checks are left untouched. Common cases that
are intentionally **not** rewritten:

- External URLs (`https://...`) -- already absolute.
- Fragment-only links (`#section`) -- in-document anchors.
- Root-absolute paths (`/docs/foo.md`) -- consumer-side, not yours.
- Paths that escape the package root via `..` -- not yours to ship.

## Why this exists

Different host tools read primitives from different directories. A single
APM package may ship instructions, prompts, agents, and skills, and each
type lands at a different destination per target (`copilot`, `claude`,
`cursor`, `codex`, ...). Without rewriting, a relative link authored
against the package layout would break the moment a primitive is
deployed.

The rewrite contract decouples your authoring layout from the host
tool's deploy layout. You write natural relative links; APM keeps them
pointing at the right files.

## Tips

- Prefer keeping closely related files inside the same skill bundle
  (`skills/<name>/`). A skill bundle preserves its internal layout when
  deployed, so links between files inside the bundle never need
  rewriting.
- For cross-bundle references (an instruction pointing at a sibling
  reference doc, a prompt pointing at a shared template), rely on the
  install-time rewrite described above.
- Sanity check after install: open the deployed file under
  `.github/instructions/` (or the equivalent for your target) and
  confirm the link points into `apm_modules/`.
