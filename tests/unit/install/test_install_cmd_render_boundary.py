"""Install command exception handlers leave rendering to the owner."""

import ast
from pathlib import Path

import pytest

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-user-facing-output-diagnostics")]


def test_install_exception_handlers_do_not_render_directly() -> None:
    """Exception handlers declare events through the command logger owner."""
    source_path = Path("src/apm_cli/commands/install.py")
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    offenders = []
    for handler in (node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)):
        for child in ast.walk(handler):
            if not isinstance(child, ast.Call) or not isinstance(child.func, ast.Name):
                continue
            if child.func.id.startswith("_rich_"):
                offenders.append((child.lineno, child.func.id))

    assert offenders == []
