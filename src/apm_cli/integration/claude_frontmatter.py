"""Claude Code agent frontmatter validation (OpenAPM req-tg-006, #5356).

The ``claude`` target copies agent files verbatim into ``.claude/agents/``.
Claude Code resolves the ``tools`` allowlist against its own tool names
(``Bash``, ``Read``, ``Edit``, ...); an entry written for another harness
(for example Copilot's ``bash``, ``view``, ``edit``) matches nothing. When
no entry resolves, Claude Code >= 2.1.208 refuses to launch the subagent
and older versions launch it without any tool.

This module inspects the ``tools`` field and reports entries that cannot
be a Claude Code tool so the install path can emit the req-tg-006
diagnostic. It does NOT mutate the frontmatter and does NOT block the
install; translation to Claude names is tracked separately.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Built-in tool names Claude Code accepts in a subagent ``tools`` list.
# Source: https://code.claude.com/docs/en/sub-agents (Available tools) and
# https://code.claude.com/docs/en/tools-reference, accessed 2026-11-24.
CLAUDE_BUILTIN_TOOLS = frozenset(
    {
        "Agent",
        "Artifact",
        "AskUserQuestion",
        "Bash",
        "BashOutput",
        "Edit",
        "EndConversation",
        "EnterPlanMode",
        "EnterWorktree",
        "ExitPlanMode",
        "ExitWorktree",
        "Glob",
        "Grep",
        "KillShell",
        "ListAgents",
        "ListPlugins",
        "ListSkills",
        "LSP",
        "Monitor",
        "MultiEdit",
        "NotebookEdit",
        "NotebookRead",
        "PowerShell",
        "Read",
        "ScheduleWakeup",
        "SearchPlugins",
        "SearchSkills",
        "SendMessage",
        "Skill",
        "SlashCommand",
        "SubagentHandback",
        "Task",
        "TaskCreate",
        "TaskGet",
        "TaskList",
        "TaskStop",
        "TaskUpdate",
        "TodoWrite",
        "ToolSearch",
        "WaitForMcpServers",
        "WebFetch",
        "WebSearch",
        "Workflow",
        "Write",
    }
)

# ``mcp__<server>``, ``mcp__<server>__*`` or ``mcp__<server>__<tool>``.
_MCP_SELECTOR_RE = re.compile(r"^mcp__[A-Za-z0-9_.-]+(?:__[A-Za-z0-9_.*-]+)?$")
# ``Bash(git status *)`` -- a tool name with a permission specifier.
_SPECIFIER_RE = re.compile(r"^([A-Za-z]+)\(.*\)$")

# Bound the number of entries named in one diagnostic so an oversized or
# hostile ``tools`` list cannot blow up a single CLI line; the rest is a count.
MAX_UNRECOGNIZED_SHOWN = 5
MAX_ENTRY_LEN = 40


@dataclass(frozen=True)
class ClaudeToolsCheck:
    """Outcome of checking a ``tools`` allowlist against Claude Code names."""

    recognized: tuple[str, ...] = field(default_factory=tuple)
    unrecognized: tuple[str, ...] = field(default_factory=tuple)
    #: True when the field was present but not a list or comma-separated string.
    malformed: bool = False

    @property
    def restricts(self) -> bool:
        return bool(self.recognized or self.unrecognized)

    @property
    def resolves_to_nothing(self) -> bool:
        """Every entry is unrecognized: Claude Code refuses to spawn the agent."""
        return bool(self.unrecognized) and not self.recognized


def is_claude_tool(entry: str) -> bool:
    """Return True when *entry* can resolve to a Claude Code subagent tool."""
    name = entry.strip()
    if not name:
        return False
    if name in CLAUDE_BUILTIN_TOOLS or _MCP_SELECTOR_RE.match(name):
        return True
    specifier = _SPECIFIER_RE.match(name)
    return bool(specifier and specifier.group(1) in CLAUDE_BUILTIN_TOOLS)


def _top_level_split(text: str) -> list[str]:
    """Split *text* on commas outside parentheses: ``Agent(a, b)`` stays one entry."""
    parts, depth, start = [], 0, 0
    for index, char in enumerate(text):
        if char == "(":
            depth += 1
        elif char == ")" and depth:
            depth -= 1
        elif char == "," and not depth:
            parts.append(text[start:index])
            start = index + 1
    parts.append(text[start:])
    return parts


def _entries(raw: object) -> list[str] | None:
    if isinstance(raw, str):
        return [part.strip() for part in _top_level_split(raw) if part.strip()]
    if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        return [item.strip() for item in raw if item.strip()]
    return None


def check_claude_tools(fm: dict | None) -> ClaudeToolsCheck:
    """Classify the ``tools`` entries of parsed agent frontmatter.

    Pure: never raises on odd shapes. ``tools`` absent, ``None`` or empty
    means no restriction (Claude Code inherits the parent's pool), so the
    returned check has ``restricts`` False.
    """
    if not isinstance(fm, dict) or "tools" not in fm or fm["tools"] is None:
        return ClaudeToolsCheck()
    entries = _entries(fm["tools"])
    if entries is None:
        return ClaudeToolsCheck(malformed=True)
    recognized = tuple(e for e in entries if is_claude_tool(e))
    unrecognized = tuple(e for e in entries if not is_claude_tool(e))
    return ClaudeToolsCheck(recognized=recognized, unrecognized=unrecognized)


def _bounded_names(entries: tuple[str, ...]) -> str:
    shown = [
        (e if len(e) <= MAX_ENTRY_LEN else e[: MAX_ENTRY_LEN - 3] + "...")
        for e in entries[:MAX_UNRECOGNIZED_SHOWN]
    ]
    text = ", ".join(shown)
    rest = len(entries) - len(shown)
    if rest > 0:
        text += f" and {rest} more"
    return text


def claude_tools_messages(check: ClaudeToolsCheck, source_name: str) -> tuple[str, str] | None:
    """Return ``(message, detail)`` for the req-tg-006 diagnostic, or None.

    *source_name* must already be sanitized for display. Entry names are
    rendered through ``ascii()`` so user frontmatter never leaks raw
    non-ASCII into CLI output.
    """
    if check.malformed:
        return (
            f"Claude agent {source_name}: frontmatter field 'tools' is not a list or "
            "comma-separated string, so tool restrictions could not be verified; the "
            "agent may inherit broader tool access.",
            "Fix: write 'tools' as a YAML list or 'tools: Read, Grep, Bash'.",
        )
    if not check.unrecognized:
        return None
    names = ascii(_bounded_names(check.unrecognized))[1:-1]
    if check.resolves_to_nothing:
        message = (
            f"Claude agent {source_name}: no entry in frontmatter field 'tools' names a "
            f"Claude Code tool ({names}); the field was kept as written, so the agent "
            "has narrower access than declared -- Claude Code >= 2.1.208 refuses to "
            "spawn it with zero tools and older versions launch it without tools."
        )
    else:
        message = (
            f"Claude agent {source_name}: frontmatter field 'tools' has entries that are "
            f"not Claude Code tools ({names}); they were kept as written and will not "
            "resolve, so the agent has narrower access than declared."
        )
    detail = (
        "Fix: use Claude Code tool names (Bash, Read, Edit, Write, Grep, Glob, Agent, "
        "Skill, mcp__<server>__*) or remove 'tools' if unrestricted access is intentional."
    )
    return message, detail
