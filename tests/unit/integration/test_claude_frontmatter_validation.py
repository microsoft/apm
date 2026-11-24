"""Pure tests for Claude Code ``tools`` allowlist classification and messages (req-tg-006, #5356)."""

from __future__ import annotations

import pytest

from apm_cli.integration.claude_frontmatter import (
    MAX_UNRECOGNIZED_SHOWN,
    check_claude_tools,
    claude_tools_messages,
    is_claude_tool,
)

pytestmark = [
    pytest.mark.unit,
    pytest.mark.promise("req-tg-006", "P-OWN-claude-agent-tools-diagnostic"),
]


class TestIsClaudeTool:
    @pytest.mark.parametrize(
        "entry",
        [
            "Bash",
            "Read",
            "Agent",
            "mcp__github",
            "mcp__github__*",
            "mcp__github__get_issue",
            "Bash(git status *)",
            " Grep ",
        ],
    )
    def test_accepts_claude_vocabulary(self, entry):
        assert is_claude_tool(entry)

    @pytest.mark.parametrize(
        "entry", ["bash", "view", "edit", "sql", "", "Unknown(x)", "mcp_github"]
    )
    def test_rejects_foreign_or_malformed_names(self, entry):
        assert not is_claude_tool(entry)


class TestCheckClaudeTools:
    def test_absent_or_null_tools_does_not_restrict(self):
        assert not check_claude_tools({}).restricts
        assert not check_claude_tools({"tools": None}).restricts
        assert not check_claude_tools(None).restricts

    def test_comma_string_and_list_are_equivalent(self):
        as_list = check_claude_tools({"tools": ["Read", "bash"]})
        as_str = check_claude_tools({"tools": "Read, bash"})
        assert as_list == as_str
        assert as_list.recognized == ("Read",)
        assert as_list.unrecognized == ("bash",)
        assert not as_list.resolves_to_nothing

    def test_all_foreign_names_resolve_to_nothing(self):
        check = check_claude_tools({"tools": ["bash", "view", "edit"]})
        assert check.resolves_to_nothing

    def test_non_list_shape_is_malformed(self):
        assert check_claude_tools({"tools": {"Read": True}}).malformed
        assert check_claude_tools({"tools": 7}).malformed


class TestClaudeToolsMessages:
    def test_no_message_when_everything_resolves(self):
        assert claude_tools_messages(check_claude_tools({"tools": ["Read"]}), "a.md") is None

    def test_zero_tool_message_names_refusal(self):
        message, detail = claude_tools_messages(
            check_claude_tools({"tools": ["bash", "view"]}), "demo.agent.md"
        )
        assert "demo.agent.md" in message
        assert "no entry" in message and "bash, view" in message
        assert "refuses to spawn" in message
        assert detail.startswith("Fix:")

    def test_partial_message_lists_only_unrecognized(self):
        message, _ = claude_tools_messages(check_claude_tools({"tools": ["Read", "sql"]}), "d.md")
        assert "(sql)" in message and "narrower access" in message

    def test_bounded_names_and_ascii_rendering(self):
        tools = [f"t{i}" for i in range(MAX_UNRECOGNIZED_SHOWN + 3)] + ["caf\u00e9"]
        message, _ = claude_tools_messages(check_claude_tools({"tools": tools}), "d.md")
        assert f"and {len(tools) - MAX_UNRECOGNIZED_SHOWN} more" in message
        assert "\u00e9" not in message

    def test_malformed_message(self):
        message, detail = claude_tools_messages(check_claude_tools({"tools": {"a": 1}}), "d.md")
        assert "could not be verified" in message
        assert "Fix:" in message and detail == ""
