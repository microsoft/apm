"""Pure Gemini agent rules (#2990): target mapping and the tool-name check.

The tool-name check mirrors ``isValidToolName`` in Gemini CLI v0.70.0:
https://github.com/google-gemini/gemini-cli/blob/v0.70.0/packages/core/src/tools/tool-names.ts
"""

from __future__ import annotations

import pytest

from apm_cli.integration.agent_integrator import is_gemini_agent_tool
from apm_cli.integration.targets import KNOWN_TARGETS

pytestmark = [pytest.mark.unit, pytest.mark.promise("req-tg-009")]


def test_gemini_profile_maps_agents_to_project_and_user_dirs() -> None:
    target = KNOWN_TARGETS["gemini"]
    agents = target.primitives["agents"]
    assert (agents.subdir, agents.extension, agents.format_id) == ("agents", ".md", "gemini_agent")
    assert agents.deploy_root is None
    # Detection and auto-create behavior are unchanged.
    assert target.detect_by_dir is True
    assert target.auto_create is False

    user = target.for_scope(user_scope=True)
    assert user is not None
    assert user.root_dir == ".gemini"
    assert "agents" in user.primitives


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("read_file", True),
        ("run_shell_command", True),
        ("search_file_content", True),
        ("*", True),
        ("mcp_*", True),
        ("mcp_github_*", True),
        ("mcp_github_create_issue", True),
        ("mcp_my.server_tool:v2", True),
        ("discovered_tool_lint", True),
        ("mcp_", False),
        ("mcp__tool", False),
        ("mcp_server", False),
        ("mcp_server___", False),
        ("mcp_*_tool", False),
        ("mcp_bad!_tool", False),
        ("Read", False),
        ("shell", False),
        ("codebase", False),
        ("", False),
    ],
)
def test_is_gemini_agent_tool_mirrors_gemini_cli(value: str, valid: bool) -> None:
    assert is_gemini_agent_tool(value) is valid
