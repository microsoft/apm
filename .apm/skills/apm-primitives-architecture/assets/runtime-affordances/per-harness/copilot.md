# Per-Harness Adapter: GitHub Copilot

Maps the substrate (../common.md) to GitHub Copilot's concrete
affordances. Load this file ONLY when a primitive declares Copilot
as a target.

Official docs cited:
- https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/create-custom-agents-for-cli
- https://docs.github.com/en/copilot/concepts/agents/coding-agent/about-hooks
- TODO: official docs needed for agent spawning / task tool syntax
- TODO: official docs needed for trigger orchestration / workflows

## 1. PERSONA SCOPING FILE

In Copilot: Custom Agent
- File extension: .agent.md
- Folder: .github/agents/ (project-local) or .copilot/agents/ (CLI user-scope)
- Frontmatter fields: name, description, model (optional)
- Activation: loaded when user selects agent from UI, or via CLI agent invocation
- Notes: agent name derived from filename (stem); agents at user scope
  visible globally to user's Copilot CLI sessions; no tool restriction
  lists (tools available per workspace/session config)
- Source: https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/create-custom-agents-for-cli

## 2. MODULE ENTRYPOINT (SKILL)

In Copilot: Skill (agentskills.io standard)
- Entrypoint file name: SKILL.md
- Folder: .github/skills/<skill_name>/ (where <skill_name> is hyphen-case,
  max 64 chars per agentskills.io)
- Frontmatter fields: name, description
- Assets folder: assets/ (arbitrary files loaded on demand from SKILL.md steps)
- Activation: description-driven matching by Copilot when user task aligns
  with skill description; also discoverable via skill commands
- Notes: skill name normalized to hyphen-case at deployment; SKILL.md
  deployed as SKILL.md at skill root (not renamed). Aligns with
  agentskills.io registry contract; description is the primary search key.
- Source: https://docs.github.com/en/copilot/customizing-copilot/adding-custom-instructions-for-github-copilot

## 3. SCOPE-ATTACHED RULE FILE

In Copilot: Instruction file
- File extension: .instructions.md
- Folder: .github/instructions/ (project-local) or .copilot/instructions/ (CLI user-scope)
- Scope mechanism: applyTo: frontmatter field (glob pattern over file paths,
  e.g. applyTo: "**/*.py" or applyTo: "src/**")
- Notes: instruction files are automatically loaded into any thread whose
  work path matches the glob. At user scope, instructions are visible to
  all projects. Pattern matching happens per-thread at runtime.
- Source: https://docs.github.com/en/copilot/customizing-copilot/adding-custom-instructions-for-github-copilot

## 4. CHILD-THREAD SPAWN

In Copilot: TODO: official docs needed
- Mechanism: TODO (likely GitHub Copilot agent tool or equivalent;
  exact syntax TBD pending official documentation)
- Parallelism: TODO
- Persona loading: TODO (child agent can reference a .agent.md?)
- Notes: Copilot agent architecture implies agent-to-agent spawning but
  concrete spawn mechanism not yet documented in accessible sources.
- Source: TODO: official docs needed

## 5. TRIGGER ORCHESTRATOR

In Copilot: TODO: official docs needed
- File format: TODO (likely .github/workflows/ YAML or equivalent)
- Trigger declaration: TODO (events, schedule, user action)
- Session bootstrap: TODO (how initial skills/agents are loaded)
- Output channel: TODO (where results are posted)
- Notes: Copilot CLI supports agents but orchestration/scheduling mechanics
  are not fully documented in current accessible GitHub docs.
- Source: TODO: official docs needed

## Capabilities Copilot lacks (vs substrate)

- Explicit child-thread spawn syntax: agent spawning is not yet publicly
  documented. Workaround: design skills with self-contained steps rather
  than fan-out (pattern P1-P4 in architecture-patterns.md); consider
  multi-agent composition via skill descriptions matching.
- Cross-session state: CLI sessions are stateless. Workaround: persist
  state to git or external store; load via task description or skill
  asset imports.
- Built-in event scheduler: Copilot CLI has no native cron. Workaround:
  use external cron/Actions to invoke copilot CLI with seeded prompts.

## Capabilities unique to Copilot (beyond substrate)

- MCP server integration: Copilot CLI and GitHub Copilot support Model
  Context Protocol (MCP) servers for tool extension via ~/.copilot/mcp-config.json.
  This is NOT part of the substrate but is a rich extension point for
  primitives that need tool access beyond native Copilot affordances.
- GitHub Actions integration: Copilot agents can be invoked from Actions
  workflows, bridging repo automation and agent reasoning.
