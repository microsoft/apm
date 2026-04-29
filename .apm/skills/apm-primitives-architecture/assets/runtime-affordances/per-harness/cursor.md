# Per-Harness Adapter: Cursor

Maps the substrate (../common.md) to Cursor's concrete affordances.
Load this file ONLY when a primitive declares Cursor as a target.

Official docs cited:
- https://docs.cursor.com/context/rules
- https://docs.cursor.com/skills
- https://docs.cursor.com/subagents
- https://docs.cursor.com/hooks

## 1. PERSONA SCOPING FILE

In Cursor: Project Rule with frontmatter, or AGENTS.md (legacy).
- File extension: .mdc (recommended) or .md (legacy AGENTS.md)
- Folder: .cursor/rules/ (project), AGENTS.md in root (legacy)
- Frontmatter fields: description, globs, alwaysApply
- Activation: Rules are loaded when alwaysApply=true (always) or
  when Agent determines relevance based on description. AGENTS.md
  is automatically loaded as project-level instructions.
- Notes: alwaysApply=false means Cursor Agent decides relevance.
  Nested AGENTS.md files are supported in subdirectories.
- Source: https://docs.cursor.com/context/rules

## 2. MODULE ENTRYPOINT (SKILL)

In Cursor: Agent Skills (partial agentskills.io compliance).
- Status: partial (SKILL.md naming supported; agentskills.io metadata not yet fully integrated)
- Closest equivalent: .agents/skills/ directory containing SKILL.md + assets/
- Standard body fields: name, description, steps with asset references
- Assets folder: .agents/skills/<skill-name>/assets/
- Activation: Agents select skills matching task description
- Notes: Cursor implements agentskills.io SKILL.md container and discovery;
  assets load on demand as agents execute steps.
- Source: https://docs.cursor.com/skills

## 3. SCOPE-ATTACHED RULE FILE

In Cursor: Project Rule with globs and alwaysApply frontmatter.
- File extension: .mdc
- Folder: .cursor/rules/
- Scope mechanism: globs field (frontmatter) for file path patterns;
  alwaysApply=true applies to all sessions; alwaysApply=false allows
  Agent to decide based on description.
- Rule types: "Always Apply", "Apply Intelligently",
  "Apply to Specific Files", "Apply Manually (@mention in chat)"
- Legacy: AGENTS.md (plain markdown, no frontmatter, auto-loaded)
- Notes: Cursor Settings UI also supports user-level rules (not file-based).
- Source: https://docs.cursor.com/context/rules

## 4. CHILD-THREAD SPAWN

In Cursor: Subagents (experimental feature).
- Mechanism: @-mention syntax in chat to dispatch work to a subagent.
  Subagents run in isolated context; return results to parent thread.
- Activation: User invocation via chat (@subagent); not programmatic
  from within running agent code.
- Async: Cursor may support parallel execution of multiple subagents;
  implementation details TODO.
- Notes: Subagents are agent-to-agent dispatch, not a task-level primitive.
  No built-in programmatic spawn from within SKILL.md step execution.
- Source: https://docs.cursor.com/subagents

## 5. TRIGGER ORCHESTRATOR

In Cursor: Hooks (.cursor/hooks/*.json) for event-driven actions.
- Mechanism: JSON hook configuration in .cursor/hooks/ directory.
  Each hook file specifies trigger event and associated action.
- Trigger types: TODO: official docs needed (repository events,
  schedule, user invocation presumed).
- Scope: Repo-local; not yet integrated with global ~/.cursor/ scope.
- Notes: Hook semantics not fully documented in official API.
- Source: https://docs.cursor.com/hooks

## 6. PLAN PERSISTENCE

In Cursor: TODO: official docs needed.
- PLAN slot: TODO (no first-class plan file documented; convention
  is to keep plans in the chat or write a markdown file the agent
  re-reads; .cursor/rules can hold persistent constraints but are
  not a session plan)
- TODO/STATUS slot: TODO (Cursor agent has no documented
  TodoWrite-equivalent tool surfaced to the model; check most
  recent docs)
- CHECKPOINT slot: TODO
- FILES slot: working directory (workspace)
- Notes: in absence of a native plan tool, the substrate-portable
  fallback is to write `plan.md` to the workspace and instruct the
  agent to re-read it at re-grounding points
- Source: TODO: official docs page for Cursor agent state /
  todo / planning tooling

## Capabilities Cursor lacks (vs substrate)

- CHILD-THREAD SPAWN: No first-class programmatic subagent spawn from
  within running agent code. Workaround: rely on user-initiated
  @-mention to delegate work; or manually compose subagent calls in
  chat UX.
- TRIGGER ORCHESTRATOR: No declarative trigger/schedule in substrate.
  Hooks are defined but semantics unclear. Workaround: use external CI
  (GitHub Actions) to invoke Cursor agent via CLI or API.
- TOKEN-CONSTRAINED PERSONA: Cursor does not support separate persona
  file isolation; personas are part of rules/AGENTS.md or skill context.

## Capabilities unique to Cursor (beyond substrate)

- MCP servers: .cursor/mcp.json for configuring Model Context Protocol
  servers (e.g. external knowledge, tools). Extends tool availability.
- User-scope rules: Cursor Settings UI supports user-level rules
  (~/.cursor/) not backed by files, complementing project rules.
- Nested AGENTS.md: Subdirectories may contain AGENTS.md files,
  each scoping instructions to that tree.
