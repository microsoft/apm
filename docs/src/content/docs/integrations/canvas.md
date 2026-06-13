---
title: "Canvas extensions (Experimental)"
description: "Ship GitHub Copilot CLI canvas extensions through APM packages (experimental, Copilot-only)."
sidebar:
  order: 8
  badge:
    text: Experimental
    variant: caution
---

:::caution[Experimental]
This feature is behind the `canvas` experimental flag and is off by default.
It is **Copilot-only**, and the CLI surface may change. Enable it explicitly
before use.
:::

A **canvas** is a GitHub Copilot CLI extension: a directory bundle whose entry
file is `extension.mjs` (executable Node.js), plus any sibling assets it needs.
Copilot CLI discovers canvases in immediate subdirectories of
`.github/extensions/<name>/` (project scope) and
`~/.copilot/extensions/<name>/` (user scope). APM lets a package carry a canvas
under `.apm/extensions/<name>/` and deploys it to the matching location at
install time so the canvas is available in your Copilot session.

Canvases are typically produced by the Copilot CLI `create-canvas` skill
(scaffolds a working extension in `.github/extensions/`). This page covers
how to ship one through an APM package.

## Enable the feature

```bash
apm experimental enable canvas
```

Default APM behaviour never changes until the flag is enabled. With the flag
off, `.apm/extensions/` is ignored entirely.

## Author a canvas

Place the bundle under your package's `.apm/` directory. The marker file is
`extension.mjs`; a directory without it is ignored.

```
.apm/
  extensions/
    my-canvas/
      extension.mjs     # required entry point (executable Node.js)
      ui.js             # optional sibling assets
      styles.css
```

The `<name>` segment becomes both the deploy directory and the extension id, so
it is validated strictly: `[A-Za-z0-9._-]+`, no leading or trailing dot, no
`..`, no path separators, and reserved device names are rejected.

## Install

```bash
apm install --target copilot
```

APM deploys the bundle verbatim to `.github/extensions/my-canvas/`. The deploy
is **atomic**: every file in the bundle is planned and validated first, and any
unmanaged local collision skips the whole bundle (use `apm install --force` to
overwrite) so you never end up with a half-updated executable extension.

After a canvas deploys, start a new Copilot CLI session (exit and relaunch) --
Copilot CLI discovers extensions at session start, so a freshly-deployed canvas
is not picked up mid-session.

## Trust gate for dependency canvases

A canvas shipped by a **dependency** is arbitrary executable Node.js code. APM
blocks dependency-provided canvases by default. To deploy them, opt in
explicitly:

```bash
apm install --target copilot --trust-canvas-extensions
```

The trust gate is independent of the experimental flag:

- The **experimental flag** decides whether the canvas primitive is processed at
  all. It is a feature-availability gate, not a security gate.
- The **`--trust-canvas-extensions` flag** decides whether *dependency*
  canvases may deploy. Your own first-party canvas (in the root package you are
  installing from) deploys freely once the flag is on; only dependency-provided
  canvases need the trust flag.

When a dependency canvas is blocked, APM prints a diagnostic naming the package,
the canvas, the `extension.mjs` entry point, the deploy directory, and the
opt-in flag. The same gate is enforced on offline bundle install
(`apm install <bundle>`) and on `apm unpack`, so a vendored bundle cannot
smuggle an executable canvas past trust.

## Install globally (user scope)

To make a canvas available in **every** Copilot session, install it globally so
it lands in `~/.copilot/extensions/<name>/`:

```bash
apm install <package> --global --trust-canvas-extensions
```

Global canvas install is intentionally limited in this experimental release:

- **Dependency-provided only.** Only a canvas shipped by a package you install
  (the `--global` flow always treats the canvas as dependency-provided) deploys
  globally, so APM records it in the user lockfile and `apm uninstall --global`
  can prune it. A first-party root `.apm/extensions/` canvas is **not** deployed
  at user scope -- package it and install it as a dependency instead.
- **Trust is always required.** A global canvas has full-account blast radius,
  so `--trust-canvas-extensions` is mandatory even though the project-scope
  first-party path does not need it.
- **Default `~/.copilot` only.** If `$COPILOT_HOME` is set to a non-default
  location, APM refuses the global canvas install rather than deploy to a path
  Copilot will not scan.

`apm uninstall --global <package>` removes the deployed
`~/.copilot/extensions/<name>/` files and prunes the empty directories.

## Pack and uninstall

`apm pack` preserves `.apm/extensions/` in the bundle, so a packed package keeps
its canvas. `apm uninstall` removes the deployed `.github/extensions/<name>/`
files and prunes the now-empty directories; uninstall is never gated by the
experimental flag, so a previously-installed canvas can always be removed.

## Scope and limitations

- **Copilot-only.** A canvas is a Copilot CLI construct. Other targets
  (`--target claude`, `cursor`, etc.) never receive it.
- **Global install is dependency-only.** User-scope (`--global`) deployment to
  `~/.copilot/extensions/` supports dependency-provided canvases (always
  requiring `--trust-canvas-extensions`) and the default `~/.copilot` location
  only; first-party root canvases deploy at project scope only.
- **No compile/list surfacing yet.** Canvases are not yet shown by
  `apm list`/`apm compile`; they are deployed at install only.
- **No policy-file control yet.** Canvas trust is controlled only by the
  `--trust-canvas-extensions` CLI flag; governing it via `apm-policy.yml` is
  planned but not part of this experimental release.

See the [primitives and targets](/apm/concepts/primitives-and-targets/) matrix
for where the canvas primitive sits.
