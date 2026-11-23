"""Ownership check for locating helper programs APM launches or probes.

``utils/git_env.trusted_executable`` is the one owner: it resolves a helper
from PATH directories outside the project (reusing
``_resolve_trusted_executable``), never from the working directory. No other
product module calls ``shutil.which``, and no ``subprocess`` launch passes a
literal program name as its first argument.
"""

from __future__ import annotations

import ast

from scripts.architecture_linter.checks.transport_platform_shared import (
    GROUP,
    _count_checks,
    _src_python,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import violation
from scripts.architecture_linter.models import Rule, Violation

_RULE_ID = "transport-platform-external-executable-owner"
_OWNER = "src/apm_cli/utils/git_env.py"
_LAUNCHERS = frozenset({"run", "Popen", "call", "check_call", "check_output"})


def _literal_program(node: ast.Call) -> bool:
    """Whether a subprocess launch names its program as a string literal."""
    if not node.args or not isinstance(node.args[0], (ast.List, ast.Tuple)):
        return False
    elements = node.args[0].elts
    return (
        bool(elements)
        and isinstance(elements[0], ast.Constant)
        and isinstance(elements[0].value, str)
    )


def _is_subprocess_launch(node: ast.Call) -> bool:
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr in _LAUNCHERS
        and isinstance(func.value, ast.Name)
        and func.value.id == "subprocess"
    )


def _check_external_executable_owner(provider: FactsProvider) -> tuple[Violation, ...]:
    """Every helper lookup and launch resolves through trusted_executable."""
    inventory = frozenset(provider.inventory)
    findings: list[Violation] = list(
        _count_checks(
            provider,
            inventory,
            _RULE_ID,
            _OWNER,
            (
                ("re", r"^def trusted_executable\(", 1, "eq"),
                ("sub", "return _resolve_trusted_executable(name)", 1, "eq"),
            ),
            "helper programs must resolve through utils/git_env.trusted_executable",
        )
    )
    for path in _src_python(provider):
        facts = provider.file_facts(path)
        launches = False
        for call in facts.calls:
            if call.qualname == "shutil.which" and path != _OWNER:
                findings.append(
                    violation(
                        _RULE_ID,
                        path,
                        "Locate helper programs with trusted_executable, not shutil.which",
                        line=call.line,
                    )
                )
            launches = launches or (
                call.qualname.startswith("subprocess.")
                and call.qualname.rsplit(".", 1)[-1] in _LAUNCHERS
            )
        if not launches:
            continue
        index = provider.tree_index(path)
        for node in index.nodes if index is not None else ():
            if (
                isinstance(node, ast.Call)
                and _is_subprocess_launch(node)
                and _literal_program(node)
            ):
                findings.append(
                    violation(
                        _RULE_ID,
                        path,
                        "Launch the path trusted_executable returns, never a bare program name",
                        line=node.lineno,
                    )
                )
    return tuple(findings)


RULES: tuple[Rule, ...] = (
    Rule(
        id=_RULE_ID,
        group=GROUP,
        guard_ids=(_RULE_ID,),
        description="Helper programs resolve on PATH outside the project, through one owner.",
        check=_check_external_executable_owner,
    ),
)

COLLECTORS: tuple[object, ...] = ()

__all__ = ["COLLECTORS", "RULES"]
