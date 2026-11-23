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
_LAUNCH_MODULES = frozenset({"subprocess", "os", "asyncio", "shutil"})


def _is_literal_argv(node: ast.AST | None) -> bool:
    """Whether *node* is a list or tuple whose first element is a string literal."""
    if not isinstance(node, (ast.List, ast.Tuple)) or not node.elts:
        return False
    first = node.elts[0]
    return isinstance(first, ast.Constant) and isinstance(first.value, str)


def _is_literal(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


_ASYNC_LAUNCHERS = frozenset({"create_subprocess_exec", "create_subprocess_shell"})
_BINDERS = frozenset({"partial", "partialmethod"})
_Names = tuple[set[str], set[str], set[str], set[str]]


def _dotted(node: ast.AST) -> str | None:
    """Return ``a.b.c`` for a Name/Attribute chain, else ``None``."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _launch_names(nodes: tuple[ast.AST, ...]) -> _Names:
    """Return local spellings of launch functions and of the subprocess, os and asyncio modules.

    Asyncio modules include ``asyncio.subprocess`` under any import spelling;
    launch functions include from-imports and local names assigned a launcher.
    """
    functions: set[str] = set()
    subprocess_modules = {"subprocess"}
    os_modules = {"os"}
    asyncio_modules = {"asyncio", "asyncio.subprocess"}
    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name
                if alias.name == "subprocess":
                    subprocess_modules.add(local)
                elif alias.name == "os":
                    os_modules.add(local)
                elif alias.name in {"asyncio", "asyncio.subprocess"}:
                    asyncio_modules.add(local)
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            functions.update(
                alias.asname or alias.name for alias in node.names if alias.name in _LAUNCHERS
            )
        elif isinstance(node, ast.ImportFrom) and node.module in {"asyncio", "asyncio.subprocess"}:
            for alias in node.names:
                if alias.name in _ASYNC_LAUNCHERS:
                    functions.add(alias.asname or alias.name)
                elif alias.name == "subprocess":
                    asyncio_modules.add(alias.asname or alias.name)
    names = (functions, subprocess_modules, os_modules, asyncio_modules)
    for node in nodes:
        if isinstance(node, ast.Assign) and _is_launcher_ref(node.value, names):
            functions.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return names


def _is_launcher_ref(expr: ast.AST, names: _Names) -> bool:
    """Whether *expr* names a program launcher under the file's import spellings."""
    functions, subprocess_modules, os_modules, asyncio_modules = names
    if isinstance(expr, ast.Name):
        return expr.id in functions
    if not isinstance(expr, ast.Attribute):
        return False
    owner, attr = _dotted(expr.value), expr.attr
    return (
        (owner in subprocess_modules and attr in _LAUNCHERS)
        or (owner in asyncio_modules and attr in _ASYNC_LAUNCHERS)
        or (owner in os_modules and attr.startswith(("exec", "spawn", "posix_spawn")))
    )


def _which_lines(nodes: tuple[ast.AST, ...]) -> list[int]:
    """Lines calling ``shutil.which`` under any import spelling."""
    modules = {"shutil"}
    functions: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.Import):
            modules.update(
                alias.asname or alias.name for alias in node.names if alias.name == "shutil"
            )
        elif isinstance(node, ast.ImportFrom) and node.module == "shutil":
            functions.update(
                alias.asname or alias.name for alias in node.names if alias.name == "which"
            )
    lines: list[int] = []
    for node in nodes:
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (isinstance(func, ast.Name) and func.id in functions) or (
            isinstance(func, ast.Attribute)
            and func.attr == "which"
            and isinstance(func.value, ast.Name)
            and func.value.id in modules
        ):
            lines.append(node.lineno)
    return lines


def _program_arg(node: ast.Call, names: _Names) -> ast.AST | None:
    """Return the argument naming the program if *node* launches one, else ``None``.

    ``partial(<launcher>, ...)`` and ``partialmethod(<launcher>, ...)`` count as
    launches with their bound arguments.
    """
    func, args = node.func, list(node.args)
    binder = (_dotted(func) or "").rpartition(".")[2]
    if binder in _BINDERS and args and _is_launcher_ref(args[0], names):
        func, args = args[0], args[1:]
    elif not _is_launcher_ref(func, names):
        return None
    if isinstance(func, ast.Attribute) and func.attr.startswith("spawn"):
        return args[1] if len(args) > 1 else None
    if args:
        return args[0]
    return next((kw.value for kw in node.keywords if kw.arg in {"args", "program", "cmd"}), None)


def _bare_launch_lines(nodes: tuple[ast.AST, ...]) -> list[int]:
    """Lines launching a program by literal name, directly or through a local name."""
    names = _launch_names(nodes)
    literal_names = {
        target.id
        for node in nodes
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and _is_literal_argv(node.value)
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
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


def _may_launch(facts: object) -> bool:
    """Cheap fact prefilter: only files that can launch or look up a program get a tree scan.

    Call facts keep the written spelling, so any call whose final attribute is
    a launcher, ``which`` or a ``partial`` binder qualifies whatever its module
    alias; a ``from`` import of a launch module covers bare-name calls.
    """
    for call in getattr(facts, "calls", ()):
        attr = call.qualname.rpartition(".")[2]
        if attr in _LAUNCHERS or attr in _ASYNC_LAUNCHERS or attr in _BINDERS or attr == "which":
            return True
        if attr.startswith(("exec", "spawn", "posix_spawn")):
            return True
    # A plain ``import subprocess`` / ``import asyncio`` can feed a local
    # launcher alias whose calls carry no launcher fact; ``os`` is left out
    # because nearly every module imports it and its launchers keep facts.
    return any(
        (fact.module is not None and fact.module.split(".")[0] in _LAUNCH_MODULES)
        or (
            fact.module is None
            and any(name.split(".")[0] in {"subprocess", "asyncio"} for name in fact.names)
        )
        for fact in getattr(facts, "imports", ())
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
        if not _may_launch(facts):
            continue
        index = provider.tree_index(path)
        nodes = tuple(index.nodes) if index is not None else ()
        if path != _OWNER:
            findings.extend(
                violation(
                    _RULE_ID,
                    path,
                    "Locate helper programs with trusted_executable, not shutil.which",
                    line=line,
                )
                for line in _which_lines(nodes)
            )
        for line in _bare_launch_lines(nodes):
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
