# Per-Harness Adapter: OpenCode

Maps the substrate (../common.md) to OpenCode's concrete
affordances. Load this file ONLY when a primitive declares
OpenCode as a target.

Official docs cited:
- https://github.com/anomalyco/opencode
- Playwright MCP snapshot: .playwright-mcp/opencode-agents.md
- Playwright MCP snapshot: .playwright-mcp/opencode-commands.md
- Playwright MCP snapshot: .playwright-mcp/opencode-config.md

## 1. PERSONA SCOPING FILE

In OpenCode: Agent configuration file
- File extension: .md (YAML frontmatter + body)
- Location: .opencode/agents/<name>.md (project)
  or ~/.config/opencode/agents/<name>.md (user)
- Frontmatter fields: name, description, model (optional),
  instructions (optional), permissions (tool access control)
- Activation: agents are listed in opencode.json config; primary
  agents displayed in UI for manual switch; subagents invoked
  by mode: subagent directive or Task tool by primary agents
- Notes: OpenCode distinguishes primary agents (main conversation)
  from subagents (specialized tasks invoked on demand). Subagents
  can run in parallel. Permissions gate tool access.
- Source: Playwright snapshot (opencode-agents.md)

## 2. MODULE ENTRYPOINT (SKILL)

In OpenCode: Agent Skill (agentskills.io standard)
- Status: SUPPORTED
- Entrypoint file name: SKILL.md
- Location: .opencode/skills/<skill_name>/ (project)
  or ~/.config/opencode/skills/<skill_name>/ (user)
- Assets: arbitrary files in assets/ subdirectory
- Activation: skills loaded into context when recommended by
  OpenCode's internal recommender (description-driven matching)
  or invoked explicitly via CLI
- Notes: OpenCode implements the agentskills.io SKILL.md
  contract. Description field drives skill discovery.
- Source: APM targets.py + Playwright snapshot

## 3. SCOPE-ATTACHED RULE FILE

In OpenCode: TODO: official docs needed
- Equivalent: PARTIAL
- Notes: OpenCode does not support scope-attached (glob-based)
  rule files. No direct equivalent to Claude Code's CLAUDE.md
  or Cursor's .cursor/rules/. Global config lives in
  opencode.json (project root or ~/.config/opencode/) but is
  not scope-triggered; it applies globally.
- Workaround: Use agent instructions field to embed context-
  specific rules as part of the agent persona. Or configure
  via opencode.json at project or user level.
- Source: Playwright snapshots + targets.py (instructions
  primitive excluded from OpenCode)

## 4. CHILD-THREAD SPAWN

In OpenCode: Subagent invocation via Task tool or mode directive
- Mechanism: Primary agent can invoke subagent via:
  (a) "mode: subagent" directive in agent config (auto-runs
      subagent as separate thread)
  (b) Task tool call from primary agent (programmatic subagent
      execution with parameters)
  (c) User manual invocation via CLI
- Parallelism: YES - Research subagent (built-in) explicitly
  supports parallel multi-task execution
- Persona loading: Subagent loads its .md file from
  .opencode/agents/ as system instructions and permission
  constraints
- Return: Child returns final response as text; parent resumes
  after child completes
- Context isolation: Child has no access to parent's context
  window except what passed in mode directive
- Notes: Subagents are first-class; visibility can be hidden
  from UI but still invoked programmatically
- Source: Playwright snapshot (opencode-agents.md)

## 5. TRIGGER ORCHESTRATOR

In OpenCode: Commands + CLI invocation (no built-in scheduler)
- Mechanism: Custom commands defined in .opencode/commands/
  or ~/.config/opencode/commands/ as .md files. User invokes
  commands in TUI or CLI. Frontmatter in command file specifies
  prompt template and behavior.
- Trigger types: User-initiated CLI/TUI command execution only
  (no native cron, timer, or webhook triggers)
- External scheduling: To achieve scheduler-driven sessions,
  use external cron/CI/webhook to invoke opencode CLI with
  initial command
- Output channel: Command output embedded in current session
  context (not a separate return channel)
- Notes: Commands are thin wrappers around prompt templates;
  they do not directly spawn independent sessions. Each command
  execution is within an existing session context.
- Source: Playwright snapshot (opencode-commands.md)

## Capabilities OpenCode lacks (vs substrate)

- Scope-attached rule files: OpenCode has no glob-based rule
  scoping. Workaround: encode rules in agent instructions or
  use global opencode.json config.
- Built-in event/timer triggers: No native scheduler for session
  bootstrap. Workaround: use external cron/CI/webhook to invoke
  CLI.
- Cross-session state persistence: Each session is isolated.
  Workaround: use external store (git, db, file) and load
  via agent instructions or config.

## Capabilities unique to OpenCode (beyond substrate)

- Parallel subagent execution: Research subagent natively runs
  multiple tasks in parallel from single parent invocation.
- Permission-gated tool access: fine-grained per-agent control
  over which tools (bash, file ops, etc.) are available.
- Built-in primary/subagent distinction: clear separation
  between main conversation agents and specialized task agents.
- Command templating: custom commands support placeholders and
  argument passing for prompt parameterization.
