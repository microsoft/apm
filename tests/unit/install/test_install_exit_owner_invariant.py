"""Source invariants for install exit-code ownership."""

from __future__ import annotations

import ast
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "src" / "apm_cli"
INSTALL_ROOT = SRC_ROOT / "install"


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _is_result_exit_code(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "exit_code"
        and isinstance(node.value, ast.Name)
        and node.value.id == "command_result"
    )


def test_only_install_command_maps_result_exit_code() -> None:
    """Exactly one command boundary translates InstallResult.exit_code."""
    owners: list[str] = []
    for path in SRC_ROOT.rglob("*.py"):
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.Call):
                continue
            if any(_is_result_exit_code(argument) for argument in node.args):
                owners.append(path.relative_to(SRC_ROOT).as_posix())

    assert owners == ["commands/install.py"]


def test_install_engine_never_calls_sys_exit() -> None:
    """Install engine modules return or raise; the command owns process exit."""
    offenders: list[str] = []
    for path in INSTALL_ROOT.rglob("*.py"):
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if (
                isinstance(function, ast.Attribute)
                and function.attr == "exit"
                and isinstance(function.value, ast.Name)
                and function.value.id == "sys"
            ):
                offenders.append(path.relative_to(SRC_ROOT).as_posix())

    assert not offenders
