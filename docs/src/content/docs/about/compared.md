---
title: How APM compares
description: Where APM fits next to npx skills, gh skill and the plugin marketplaces built into Claude Code, GitHub Copilot and Codex.
---

You probably already install agent capabilities some other way, through your harness (the
agent tool, such as Claude Code or GitHub Copilot) or a command-line installer. This page explains what each
approach does well and where APM adds something, so you can decide whether you need it. Each tool
changes quickly; check its own documentation before you rely on a detail. This page reflects
November 2026.

## The short version

Most tools here answer "how do I get this skill onto my machine?". APM answers a different
question: "how does my whole team, and our CI, get exactly the same agent capabilities, from
sources we allow, on every harness we use?". They compose well: discover a skill wherever you
like, then manage it with APM.

| You want to... | Good fit |
|---|---|
| Try a skill on your own machine, once | The tool built into your harness, or `npx skills add` |
| Pin one skill to a commit for one harness | `gh skill` or your harness's plugin pin |
| Give a team the same skills, plugins and MCP servers on several harnesses, reproducibly | APM |
| Restrict which sources agents may load from, and check it on every pull request | APM |
| Run a private marketplace of approved agent capabilities for your organization | APM, with your existing git host |

## npx skills

[Vercel's `skills`](https://github.com/vercel-labs/skills) installs Agent Skills into dozens of
agents, and [skills.sh](https://skills.sh) is a large public directory to find them. It copies
skills into your agent folders.

APM accepts the same gesture, `apm install owner/repo` or `apm install owner/repo --skill name`,
and adds a manifest, a lockfile with per-file hashes, transitive dependencies, and the other agent
capabilities: instructions, agents, hooks, MCP servers and plugins.
[Coming from npx skills](../../getting-started/coming-from/).

## gh skill

The GitHub CLI's `gh skill` installs skills and pins them to a commit SHA for several agents. If
pinning one skill is all you need, it does the job.

APM pins the same way (`owner/repo#<sha>` or a tag) and records it in a lockfile that covers every
dependency, plus policy and audit for teams.

## Plugin marketplaces built into harnesses

Claude Code, GitHub Copilot and Codex can add plugin marketplaces and install plugins natively.
Claude Code also resolves plugin dependencies. These features install plugins and switch them on;
they do not record which exact files each machine received, and each harness keeps its own
settings.

APM installs from the same marketplaces (`apm install name@marketplace`), locks what arrived,
applies your organization's policy and approvals, and gives you one manifest across harnesses.
[Install plugins](../../consumer/install-plugins/).

## What APM is not

- Not a harness or an agent runtime. Your harness runs the agent and decides what it may do.
- Not a hosted registry or a required marketplace. Packages come from git.
- Not a security scanner. APM checks files against the lockfile and flags hidden Unicode
  characters; it does not judge whether content is malicious.
  [Agent supply chain security model](../../enterprise/security/).
