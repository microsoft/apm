---
title: "Marketplace upstreams"
description: Selectively expose plugins from external marketplaces with allow-list governance and immutable commit pinning.
sidebar:
  order: 7
---

Upstreams are APM's equivalent of Artifactory remote repositories -- they let your internal marketplace selectively expose plugins from external sources, with allow-list governance and immutable commit pinning, without running an artifact server.

This guide is for **marketplace curators** who want to re-expose plugins from a third-party marketplace (for example, a public Claude Code marketplace) inside their own marketplace, with control over which plugins are exposed and at what version. If you are authoring a marketplace from scratch, start with the [Authoring a marketplace](../marketplace-authoring/) guide first.

## Trust model

Upstreams are a **curated pass-through**, not a binary mirror.

| Concern | v1 status |
|---|---|
| Allow-list governance (curator picks which plugins are exposed) | Yes |
| Build-time commit pinning (manifest SHA + plugin SHA in lockfile) | Yes |
| Reproducible curator builds (rebuild from lock = byte-identical output) | Yes |
| Defence against upstream repo rename / takeover | Yes (canonical-owner check) |
| Consumer-side artifact custody | No -- consumer clones from upstream git host at install |
| Resilience to upstream takedown / force-push | No -- consumer install fails if upstream rewrites history |

Air-gapped re-hosting is **out of scope for v1** and is tracked separately as a future `distribution: rehost` mode.

## Quick start

Register an external marketplace under a local alias, then expose one of its plugins:

```bash
# 1. Register the upstream marketplace, pinned to an immutable commit.
#    Use a real 40-char SHA (preferred over tags -- tags can be re-pointed).
#    Get the current SHA with:  git ls-remote https://github.com/abhigyanpatwari/GitNexus HEAD
apm marketplace upstream add abhigyanpatwari/GitNexus \
  --alias gitnexus \
  --ref cbe5dac8b7590efc11445032cd1ded6b64fe784e

# 2. Confirm the upstream is registered in apm.yml
apm marketplace upstream list

# 3. Expose one plugin under your own display name
apm marketplace package add \
  --upstream gitnexus \
  --plugin gitnexus \
  --name acme-gitnexus

# 4. Build your marketplace.json
apm pack
```

The emitted `marketplace.json` is byte-for-byte Anthropic-conformant -- it does **not** carry any APM-specific keys. Provenance (manifest SHA, resolved plugin SHA, canonical owner) is recorded only in your `apm.lock.yaml` under the `upstreams:` section.

## Schema

In `apm.yml`:

```yaml
marketplace:
  upstreams:
    - alias: gitnexus
      repo: abhigyanpatwari/GitNexus
      path: .claude-plugin/marketplace.json    # default
      ref: <sha-or-tag>                         # required for reproducibility
      branch: main                              # used only with allow_head
      host: github.com                          # default
      allow_head: false                         # default; opt-in to mutable refs
  packages:
    # Direct package
    - name: my-skill
      source: owner/repo
      version: ">=1.0.0"

    # Upstream-sourced package
    - name: acme-gitnexus              # display name in your marketplace
      upstream: gitnexus               # references upstreams[].alias
      plugin: gitnexus                 # name in the upstream marketplace
      version: ">=1.0.0"               # optional curator override
```

`upstream` and `source` are mutually exclusive on a single `packages[]` entry.

## CLI reference

| Command | Purpose |
|---|---|
| `apm marketplace upstream add <repo> --alias <alias> --ref <sha>` | Register an upstream marketplace pinned to a 40-char SHA (recommended) |
| `apm marketplace upstream add <repo> --alias <alias> --ref v1.2.3` | Pin to an annotated tag (acceptable for stable upstreams; SHA still preferred) |
| `apm marketplace upstream add <repo> --alias <alias> --branch main --allow-head` | Track a mutable branch -- requires explicit `--allow-head` opt-in (warned every build) |
| `apm marketplace upstream list` | List registered upstreams |
| `apm marketplace upstream remove <alias>` | Remove an upstream (rejects if any package still references it) |
| `apm marketplace package add --upstream <alias> --plugin <name> [--name ...]` | Expose an upstream plugin in your `packages[]` |

### Tag vs SHA: when to use which

- **Always prefer a 40-char SHA.** It is content-addressed: even if the upstream force-pushes the branch the tag points at, your build keeps resolving the original tree.
- **Tags are acceptable** when the upstream maintainer has a strong stable-tag discipline (annotated, signed, never moved). APM still resolves the tag to its current SHA at build time and writes the resolved SHA to `apm.lock.yaml` -- so reproducibility holds for that lockfile, but a fresh `add` after the tag moves will resolve to a new SHA.
- **Branches** (`--branch main --allow-head`) are explicitly opt-in. Every build emits a warning, and enterprise policy can reject HEAD-tracking entries entirely.

## Reproducibility

Every build pins:

- The **upstream `marketplace.json` commit SHA** (so the manifest itself can't change under you).
- Each **upstream plugin's resolved commit SHA** (so the plugin source code can't change under you).

These pins are written to `apm.lock.yaml` under `upstreams:`. Subsequent rebuilds replay from the lock and produce byte-identical output. To advance the pins, re-run the relevant `apm marketplace upstream add` (or, in a future release, `apm marketplace upstream refresh`).

## Failure modes

The builder fails closed on every upstream-resolution problem (exit code `2`) rather than silently skipping. You will see one of these named errors:

```text
[x] upstream alias 'gitnexus' is not declared in marketplace.upstreams
```

The `--upstream` value on a `packages[]` entry must reference a declared `upstreams[].alias`. Add the upstream first, or fix the typo in the package entry.

```text
[x] upstream 'gitnexus' canonical name has changed: declared 'old-owner/GitNexus' but GitHub returns 'new-owner/GitNexus' (possible repo rename or takeover)
```

The repo at the configured owner/repo path no longer matches what your lockfile recorded the last time the upstream was refreshed. Investigate before advancing the pin -- this is the same signal package-confusion attacks produce.

```text
[x] upstream 'gitnexus' resolves to ref 'main' which is a moving branch; pass --allow-head to opt in or pin --ref to a SHA / tag
```

You attempted to register or build an upstream against a branch without explicit `--allow-head`. Either pin to an immutable SHA / tag (recommended) or opt in to HEAD-tracking with `--allow-head` and accept the per-build warning.

## What is NOT supported in v1

- **Re-hosting / artifact custody.** Consumer installs always fetch plugin content from the upstream git host. APM never proxies or stores that content.
- **Transitive upstreams.** If marketplace B upstreams marketplace C, you cannot upstream B and inherit C transitively.
- **Cross-host upstreams.** The upstream and its referenced plugins must live on the same git host family (for example, both on github.com).
- **Search.** `apm marketplace upstream list` covers discovery in v1.

## Related

- [Authoring a marketplace](../marketplace-authoring/) -- start here if your `apm.yml` has no `marketplace:` block yet.
- [Marketplaces (consumer)](../marketplaces/) -- registering and consuming external marketplaces from a project.
- [Security and trust](../../enterprise/security/) -- the full APM security model.
