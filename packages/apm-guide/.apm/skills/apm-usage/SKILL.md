---
name: apm-usage
description: >
  Activate when the user asks about APM (Agent Package Manager): installing,
  configuring, authoring, or troubleshooting AI-agent packages, dependencies,
  compilation, MCP servers, policy, or any `apm` CLI command.
---

# APM Usage

APM (Agent Package Manager) is the open-source package manager for AI coding
agents. It lets teams install, share, and govern reusable instructions, prompts,
agents, skills, and MCP server configurations across projects.

## When to activate

- User mentions `apm` or "Agent Package Manager"
- Questions about installing or managing AI-agent packages
- Setting up instructions, prompts, agents, skills, or chatmodes
- Configuring MCP servers through apm.yml
- Authentication for private repos (GitHub, ADO, GHES, Artifactory)
- Policy enforcement or `apm audit`
- Package authoring or publishing
- Compiling agent context (`apm compile`)
- Troubleshooting apm errors

## Key rules

- **Commit these files:** apm.yml, apm.lock.yaml, .apm/, .github/, .claude/, .cursor/
- **Never commit:** apm_modules/ (add to .gitignore)
- **Team sync:** after `git clone`, run `apm install` to restore dependencies
- **Update deps:** `apm install --update` refreshes to latest refs
- **Pin versions:** use tags (`#v1.0.0`) in production, branches for development
- **ASCII only:** all CLI output and source must stay within printable ASCII

## Reference

For detailed guidance, see the following resources:

- [Installation](./installation.md) -- install and update APM
- [Workflow](./workflow.md) -- core workflow, apm.yml format, what to commit
- [Commands](./commands.md) -- full CLI command reference
- [Dependencies](./dependencies.md) -- all dependency formats and version pinning
- [Authentication](./authentication.md) -- token setup for private repos
- [Governance](./governance.md) -- policy engine and audit checks
- [Package Authoring](./package-authoring.md) -- creating APM packages
- [Troubleshooting](./troubleshooting.md) -- common errors and fixes
