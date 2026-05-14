---
title: Package-relative links
description: How relative markdown links inside an APM package survive deployment to consumer harness directories.
---

A primitive in your package can link to a sibling file with a normal
relative markdown link. APM rewrites that link at install time so it
still resolves after the primitive is copied into the consumer's
harness directory (`.claude/`, `.github/`, `.cursor/`, ...) -- far
away from the package's authored layout.

This page is the contract: which links get rewritten, which do not,
and how to verify the result.

## The rewrite, in one example

Author layout in your package:

```
my-pkg/
+-- apm.yml
+-- .apm/
|   +-- skills/code-review/SKILL.md
|   +-- agents/architect.agent.md
+-- references/
    +-- patterns.md
```

`architect.agent.md` links to a sibling reference:

```markdown
See [design patterns](../../references/patterns.md).
```

After `apm install` in a consumer with the `claude` target, the
deployed file `.claude/agents/architect.md` reads:

```markdown
See [design patterns](../../apm_modules/<owner>/my-pkg/references/patterns.md).
```

The package itself stays intact under `apm_modules/`; the deployed
primitive points back into it.

## What APM rewrites

A markdown link `[text](path)` is rewritten only when **all** hold
(`src/apm_cli/compilation/link_resolver.py:391-529`):

1. The link is relative -- no URL scheme (`http:`, `mailto:`, ...), not
   protocol-relative (`//host`), not root-absolute (`/foo`), not a bare
   fragment (`#section`).
2. The resolved target is a regular file that exists on disk.
3. The resolved target stays inside the source package root (validated
   against symlink and `..` traversal).

Anything else is passed through verbatim. This applies to
**instructions, prompts, agents, and commands**. Skills are different
-- see below.

`#fragment` and `?query` suffixes are preserved through the rewrite.

## What APM does not rewrite

| Link form | Why it is left alone |
|---|---|
| `https://example.com/...` | External URL, already absolute. |
| `#section` | In-document anchor; nothing to resolve. |
| `/docs/foo.md` | Root-absolute -- consumer-side, not yours. |
| `../../../outside.md` | Escapes the package root; refused. |
| `mailto:`, `file:`, custom schemes | Not filesystem paths. |
| Relative paths whose target file does not exist | Cannot verify; treated as intentional. |

Cross-package references are not supported. A primitive in package A
cannot link into package B -- the resolved path escapes A's root and
the link is left unrewritten. Packages are independent deployment
units. If you need shared content, depend on the other package and
inline what you need.

## Skills are a special case

Skills deploy as whole bundles (`shutil.copytree` in
`src/apm_cli/integration/skill_integrator.py:392`). The internal
layout of a skill folder is preserved as-is at the destination, so
**links between files inside a single skill bundle just work and are
not rewritten**.

Author layout:

```
my-pkg/.apm/skills/code-review/
+-- SKILL.md
+-- references/api.md
+-- scripts/lint.sh
```

`SKILL.md` linking to `references/api.md` or `scripts/lint.sh` keeps
working unchanged after deploy to `.claude/skills/code-review/`,
`.codex/skills/code-review/`, etc.

A link from inside a skill bundle out to the package's other
directories (e.g. `../../references/patterns.md`) does go through the
rewriter, with the same rules as above.

## Verify after install

Install your package into a scratch consumer and inspect a deployed file:

```bash
mkdir scratch && cd scratch
apm init
apm install ../path/to/my-pkg
grep -E '\]\(' .claude/agents/architect.md
ls apm_modules/*/my-pkg/references/patterns.md
```

The rewritten paths should point into `apm_modules/<owner>/my-pkg/`
and the targets should exist.

For the full reach map of which primitive lands where on each harness,
see [Primitives and targets](../../concepts/primitives-and-targets/).
