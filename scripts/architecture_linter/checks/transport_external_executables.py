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
_LAUNCH_MODULES = frozenset({"subprocess", "os", "asyncio"})


def _is_literal_argv(node: ast.AST | None) -> bool:
    """Whether *node* is a list or tuple whose first element is a string literal."""
    if not isinstance(node, (ast.List, ast.Tuple)) or not node.elts:
        return False
    first = node.elts[0]
    return isinstance(first, ast.Constant) and isinstance(first.value, str)


def _is_literal(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _launch_names(nodes: tuple[ast.AST, ...]) -> tuple[set[str], set[str], set[str]]:
    """Return local spellings of subprocess launchers, the subprocess module and os."""
    functions: set[str] = set()
    subprocess_modules = {"subprocess"}
    os_modules = {"os"}
    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    subprocess_modules.add(alias.asname or alias.name)
                elif alias.name == "os":
                    os_modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            functions.update(
                alias.asname or alias.name for alias in node.names if alias.name in _LAUNCHERS
            )
    return functions, subprocess_modules, os_modules


def _program_arg(node: ast.Call, names: tuple[set[str], set[str], set[str]]) -> ast.AST | None:
    """Return the argument naming the program if *node* launches one, else ``None``."""
    functions, subprocess_modules, os_modules = names
    func = node.func
    first = node.args[0] if node.args else None
    if isinstance(func, ast.Name):
        return first if func.id in functions else None
    if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
        return None
    owner, attr = func.value.id, func.attr
    if owner in subprocess_modules and attr in _LAUNCHERS:
        return first
    if owner == "asyncio" and attr == "create_subprocess_exec":
        return first
    if owner in os_modules and attr.startswith(("exec", "posix_spawn")):
        return first
    if owner in os_modules and attr.startswith("spawn"):
        return node.args[1] if len(node.args) > 1 else None
    return None


def _bare_launch_lines(nodes: tuple[ast.AST, ...]) -> list[int]:
    """Lines launching a program by literal name, directly or through a local name."""
    names = _launch_names(nodes)
    literal_names = {
        target.id
        for node in nodes
        if isinstance(node, ast.Assign) and _is_literal_argv(node.value)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    lines: list[int] = []
    for node in nodes:
        if not isinstance(node, ast.Call):
            continue
        program = _program_arg(node, names)
        launches = program is not None or any(
            keyword.arg == "executable" for keyword in node.keywords
        )
        if not launches:
            continue
        bare = (
            _is_literal_argv(program)
            or _is_literal(program)
            or (isinstance(program, ast.Name) and program.id in literal_names)
            or any(
                keyword.arg == "executable" and _is_literal(keyword.value)
                for keyword in node.keywords
            )
        )
        if bare:
            lines.append(node.lineno)
    return lines


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
        if not any(
            (fact.module or "").split(".")[0] in _LAUNCH_MODULES
            or any(name.split(".")[0] in _LAUNCH_MODULES for name in fact.names)
            for fact in facts.imports
        ):
            continue
        index = provider.tree_index(path)
        for line in _bare_launch_lines(tuple(index.nodes) if index is not None else ()):
            findings.append(
                violation(
                    _RULE_ID,
                    path,
                    "Launch the path trusted_executable returns, never a bare program name",
                    line=line,
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
