---
title: Use APM packages
description: Install, manage, and run APM packages in your project.
sidebar:
  order: 0
---

You're here because you want to install someone else's APM packages and use them in your project. This is the consumer ramp.

## Where to start

| Your situation                                                                 | Start here                                              |
|--------------------------------------------------------------------------------|---------------------------------------------------------|
| First time using APM, just want to try it                                      | [Quickstart (5 min)](../quickstart/)                    |
| Adding APM to an existing repo                                                 | [Install packages](./install-packages/)                 |
| You hit `Authentication failed` for a private repo                             | [Authentication](./authentication/)                     |
| You need org-private packages or your own marketplace                          | [Private and org packages](./private-and-org-packages/) |
| You manage `apm.yml` and need lockfile / dependency commands                   | [Manage dependencies](./manage-dependencies/)           |
| You want APM to wire MCP servers (GitHub, Atlassian, ...) into your tools      | [Install MCP servers](./install-mcp-servers/)           |
| You received a local `.tar.gz` bundle and need to install it                   | [Deploy a local bundle](./deploy-a-bundle/)             |
| You hit `Drift detected` after a `git pull`                                    | [Drift and secure-by-default](./drift-and-secure-by-default/) |
| Your org rolled out `apm-policy.yml` and your install is now blocked           | [Governance on the consumer ramp](./governance-on-the-consumer-ramp/) |

## The consumer flow

The four commands you'll use almost every day:

```bash
apm init                 # one-time per project
apm install <pkg>        # add a dependency
apm install              # restore from apm.lock.yaml
apm run <script>         # invoke a script declared in apm.yml
```

That's the loop. Everything else is either lifecycle automation (`update`, `outdated`, `audit`) or a workflow extension (MCP servers, local bundles, scripts).

## Recommended reading order

1. [Install packages](./install-packages/) -- the canonical install loop.
2. [Manage dependencies](./manage-dependencies/) -- `apm.yml`, lockfile, `update`, `outdated`.
3. [Run scripts](./run-scripts/) -- the script runner that wraps your agent runtime of choice.
4. [Update and refresh](./update-and-refresh/) -- when refs move, when caches go stale.
5. Stop here unless you hit one of the situational pages above.

## Producer-curious?

If you want to *publish* a package others can install, switch to the [producer ramp](../producer/). The skills you build there install on the same `apm install` command everyone here is running.

## Enterprise rollout?

If you operate a platform team and need org-wide policy, audit, and CI gating, the [enterprise ramp](../enterprise/) is the right entry. Consumer commands are unchanged when policy is enforced -- you'll just see more `[x]` blocks at install time when something is denied.
