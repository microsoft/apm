"""Source invariants for the canonical target capability owner."""

import ast
import re
from pathlib import Path

import pytest

COMMAND_SOURCES = (
    Path("src/apm_cli/commands/install.py"),
    Path("src/apm_cli/commands/update.py"),
    Path("src/apm_cli/commands/compile/cli.py"),
)
TARGET_NAMES = (
    "agent-skills",
    "agents",
    "agy",
    "antigravity",
    "claude",
    "codex",
    "copilot",
    "copilot-app",
    "copilot-cowork",
    "cursor",
    "gemini",
    "hermes",
    "intellij",
    "kiro",
    "openclaw",
    "opencode",
    "vscode",
    "windsurf",
)
TARGET_LIST_PATTERN = re.compile(
    rf"\b(?:{'|'.join(map(re.escape, TARGET_NAMES))})"
    rf"(?:[`,]?\s*,\s*[`']?(?:{'|'.join(map(re.escape, TARGET_NAMES))})[`,]?){{2,}}"
)


@pytest.mark.parametrize("source_path", COMMAND_SOURCES)
def test_target_help_has_no_handwritten_accepted_value_list(source_path: Path) -> None:
    """Target command help must render accepted values from the catalog."""
    source = source_path.read_text(encoding="utf-8")
    assert "target_help_fragment" in source
    string_literals = (
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )
    assert all(TARGET_LIST_PATTERN.search(value) is None for value in string_literals)
