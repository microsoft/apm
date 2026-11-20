"""Advice never names a way around org policy (RA-015).

Every remediation in the catalog, and every refusal APM prints, names the fix
for its cause. ``--no-policy`` and ``APM_POLICY_DISABLE`` may only appear where
APM declares the escape hatch itself (the CLI options, the notice that it is
active, the reader of the variable) or reports that policy was not consulted.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from apm_cli.core.remediation import catalog

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-remediation-advice")]

_BYPASS = ("--no-policy", "APM_POLICY_DISABLE")
_SRC = Path(__file__).resolve().parents[3] / "src" / "apm_cli"
# (file, function): places that declare or report the escape hatch, never advise it.
_DECLARES_THE_ESCAPE_HATCH = frozenset(
    {
        ("commands/audit.py", "audit"),
        ("commands/audit.py", "_audit_ci_gate"),
        ("commands/install.py", "install"),
        ("commands/lock.py", "lock"),
        ("install/phases/policy_gate.py", "_is_policy_disabled"),
        ("policy/ci_checks.py", "_check_exec_status_current"),
        ("policy/discovery.py", "policy_disabled_by_env"),
        ("policy/install_preflight.py", "run_policy_preflight"),
    }
)


def test_no_remediation_advises_a_policy_bypass() -> None:
    offenders = [
        entry.id
        for entry in catalog().values()
        for text in (entry.command, entry.fixes_promise, entry.before or "", entry.then or "")
        if any(bypass in text for bypass in _BYPASS)
    ]
    assert offenders == []


def _docstrings(tree: ast.AST) -> set[int]:
    nodes = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    return {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, nodes)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }


def _bypass_literals(path: Path) -> list[tuple[str, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = _docstrings(tree)
    owner: dict[int, str] = {}
    for function in ast.walk(tree):
        if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(function):
                owner.setdefault(id(node), function.name)
    relative = path.relative_to(_SRC).as_posix()
    return [
        (relative, owner.get(id(node), "<module>"))
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
        and any(bypass in node.value for bypass in _BYPASS)
    ]


def test_no_refusal_string_advises_a_policy_bypass() -> None:
    found = {site for path in sorted(_SRC.rglob("*.py")) for site in _bypass_literals(path)}
    assert sorted(found - _DECLARES_THE_ESCAPE_HATCH) == []
