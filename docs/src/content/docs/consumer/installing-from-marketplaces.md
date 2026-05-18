---
title: Installing from marketplaces
description: Six ways consumers install APM-published plugins -- through APM, VS Code, Cursor, Copilot CLI, Claude Code, and Codex -- and the auth and cache layout of each.
sidebar:
  order: 2
---

A marketplace publisher (see
[Publish to a marketplace](../../producer/publish-to-a-marketplace/))
ships one `marketplace.json` that every major agent runtime can read.
This page covers the install side: which command each runtime
exposes, what authentication it needs, and where files land on disk.

The recommended path is `apm install`. It is the only path that gives
you a committed `apm.lock.yaml`, content-hash pinning, transitive
resolution, the pre-install security scan, and `apm audit --ci`
drift detection. Use a native command when you cannot install APM,
or when the runtime owns the install surface (VS Code Copilot Chat).

## Install patterns

| Runtime                         | Install command                                                         | Auth                                | Cache layout                                                                 |
|---------------------------------|-------------------------------------------------------------------------|-------------------------------------|------------------------------------------------------------------------------|
| APM (recommended)               | `apm marketplace add <owner>/<repo>` then `apm install <pkg>@<marketplace>` | Host token (`GITHUB_APM_PAT`, etc.) via git credential helper. | `apm_modules/` in the project; `~/.apm/cache/` for fetched refs.             |
| VS Code (GitHub Copilot Chat)   | Plugin marketplace UI; or `code --install-extension` for the marketplace itself. | VS Code GitHub sign-in.             | Per-user extension store managed by VS Code. APM artifacts stream from the marketplace at activation. |
| Cursor                          | Settings -> Plugins -> add marketplace URL.                             | Cursor account.                     | `~/.cursor/extensions/` per-user.                                            |
| GitHub Copilot CLI              | `gh copilot marketplace add <owner>/<repo>` then `gh copilot plugin install <pkg>`. | `gh auth login`.                    | `~/.config/gh/copilot/` per-user.                                            |
| Claude Code                     | `/plugin marketplace add <owner>/<repo>` then `/plugin install <pkg>`.  | Anthropic sign-in.                  | `~/.claude/plugins/` per-user; `.claude/` in-project when wired.             |
| OpenAI Codex CLI                | `codex plugin add <owner>/<repo>` then `codex plugin install <pkg>`.    | OpenAI sign-in.                     | `~/.codex/plugins/` per-user; `.codex/` in-project when wired.               |

Native commands above read whichever artifact the runtime expects.
Most read `.claude-plugin/marketplace.json` (Anthropic-compatible
schema); Codex reads `.agents/plugins/marketplace.json`. Producers
who enable both outputs reach every runtime from one repo.

## What you give up with native installs

Native runtime commands install the plugin and stop there. They do
not produce a project-scoped lockfile, do not run the APM security
scan, and do not participate in `apm audit --ci` drift detection. If
your team or org has adopted APM, prefer `apm install` even when a
native command exists -- the lockfile is what makes installs
reproducible across machines and CI.

| Capability                          | `apm install` | Native runtime install |
|-------------------------------------|---------------|------------------------|
| Project-scoped `apm.lock.yaml`      | yes           | no                     |
| Content-hash pinning                | yes           | no                     |
| Transitive dependency resolution    | yes           | no                     |
| Pre-install security scan           | yes           | no                     |
| `apm audit --ci` drift gate         | yes           | no                     |
| Cross-harness deploy from one ref   | yes           | no (per-runtime install) |

## Picking the install path

- **You are the only user on a workstation and the plugin is simple.**
  A native install is fine. Treat it as a personal tool, not project
  infrastructure.
- **The plugin will be used by a team or in CI.** Use `apm install`.
  Commit `apm.yml` and `apm.lock.yaml`. Every contributor gets the
  same bytes.
- **Your org has an `apm-policy.yml`.** `apm install` is the only
  path that enforces it. See
  [Governance on the consumer ramp](../governance-on-the-consumer-ramp/).

## Where next

- [Install packages](../install-packages/) -- the full `apm install`
  surface for declared dependencies.
- [Authentication](../authentication/) -- tokens for private hosts.
- [Publish to a marketplace](../../producer/publish-to-a-marketplace/) --
  the producer side of the artifacts described above.
