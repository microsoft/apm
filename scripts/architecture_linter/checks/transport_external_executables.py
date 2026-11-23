"""Ownership check for locating helper programs APM launches or probes.

``utils/git_env.trusted_executable`` is the one owner: it resolves a helper
from PATH directories outside the project (reusing
``_resolve_trusted_executable``), never from the working directory. No other
product module calls ``shutil.which``, and no ``subprocess`` launch passes a
literal program name as its first argument.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable
from dataclasses import dataclass

from scripts.architecture_linter.checks.transport_platform_shared import (
    GROUP,
    _count_checks,
    _src_python,
)
from scripts.architecture_linter.checks.tree_index import TreeIndex
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
_PROGRAM_KEYWORDS = frozenset({"args", "program", "cmd"})
# An os launcher named without being called, e.g. ``launch = os.execv``.
_OS_LAUNCHER_REF = re.compile(r"\.(?:exec|spawn)[lv]p?e?\b|\bposix_spawnp?\b")


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


def _os_program_index(attr: str) -> int | None:
    """Return the program argument index of ``os.<attr>``, if it launches one."""
    if attr.startswith("spawn"):
        return 1
    if attr.startswith(("exec", "posix_spawn")):
        return 0
    return None


@dataclass(frozen=True)
class _Modules:
    """File-wide spellings of the launch modules and of the functools binders."""

    subprocess: frozenset[str]
    os: frozenset[str]
    asyncio: frozenset[str]
    binders: frozenset[str]


def _modules(nodes: tuple[ast.AST, ...]) -> _Modules:
    """Collect every local spelling of subprocess, os, asyncio(.subprocess) and the binders."""
    spellings: dict[str, set[str]] = {
        "subprocess": {"subprocess"},
        "os": {"os"},
        "asyncio": {"asyncio", "asyncio.subprocess"},
        "binders": set(),
    }
    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name
                if alias.name in {"subprocess", "os"}:
                    spellings[alias.name].add(local)
                elif alias.name == "asyncio":
                    spellings["asyncio"].update({local, f"{local}.subprocess"})
                elif alias.name == "asyncio.subprocess":
                    spellings["asyncio"].add(local)
                elif alias.name == "functools":
                    spellings["binders"].update(f"{local}.{name}" for name in _BINDERS)
        elif isinstance(node, ast.ImportFrom) and node.module in {"asyncio", "functools"}:
            for alias in node.names:
                if node.module == "asyncio" and alias.name == "subprocess":
                    spellings["asyncio"].add(alias.asname or alias.name)
                elif node.module == "functools" and alias.name in _BINDERS:
                    spellings["binders"].add(alias.asname or alias.name)
    return _Modules(**{key: frozenset(value) for key, value in spellings.items()})


def _imported_program_index(module: str | None, name: str) -> int | None:
    """Return the program argument index of ``from <module> import <name>``."""
    if module == "subprocess" and name in _LAUNCHERS:
        return 0
    if module in {"asyncio", "asyncio.subprocess"} and name in _ASYNC_LAUNCHERS:
        return 0
    if module == "os":
        return _os_program_index(name)
    return None


class _Aliases:
    """Launcher aliases per lexical scope, each keeping its launcher's program argument.

    Names bind in their function, class or module scope and resolve through
    enclosing functions to the module, as Python resolves them; attribute
    targets (``self.run = ...``) bind per owning class. A binding maps to the
    index of the program argument, or ``None`` when it is not a launcher.
    """

    def __init__(self, index: TreeIndex | None, nodes: tuple[ast.AST, ...], modules: _Modules):
        self._index = index
        self._modules = modules
        self._names: dict[tuple[int, str], int | None] = {}
        self._attributes: dict[tuple[int, str], int | None] = {}
        self._classes: dict[int, int] = {}
        assignments: list[tuple[ast.AST, list[ast.expr], ast.expr]] = []
        for node in nodes:
            self._bind(node)
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                assignments.append((node, targets, node.value))
        # Aliases may chain through each other in any order: resolve to a fixpoint.
        changed = True
        while changed:
            changed = False
            for node, targets, value in assignments:
                program = self.program_index(value, node)
                if program is None:
                    continue
                for target in targets:
                    changed = self._set(target, node, program) or changed

    def _anchor(self, node: ast.AST | None) -> ast.AST | None:
        return None if node is None or self._index is None else self._index.definition_anchor(node)

    def _owning_class(self, node: ast.AST) -> int:
        """Return the id of the nearest class enclosing *node*, or 0 at module level."""
        anchor = self._anchor(node)
        key = id(anchor)
        if key not in self._classes:
            scope = anchor
            while scope is not None and not isinstance(scope, ast.ClassDef):
                scope = self._anchor(scope)
            self._classes[key] = id(scope) if scope is not None else 0
        return self._classes[key]

    def _scope(self, node: ast.AST) -> int:
        anchor = self._anchor(node)
        return id(anchor) if anchor is not None else 0

    def _bind(self, node: ast.AST) -> None:
        """Record every name or attribute *node* binds, as not (yet) a launcher."""
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            self._names.setdefault((self._scope(node), node.id), None)
        elif isinstance(node, ast.arg):
            self._names.setdefault((self._scope(node), node.arg), None)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            self._names.setdefault((self._scope(node), node.name), None)
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
            spelled = _dotted(node)
            if spelled is not None:
                self._attributes.setdefault((self._owning_class(node), spelled), None)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            module = node.module if isinstance(node, ast.ImportFrom) else None
            for alias in node.names:
                local = (alias.asname or alias.name).split(".")[0]
                program = None
                if isinstance(node, ast.ImportFrom):
                    program = _imported_program_index(module, alias.name)
                key = (self._scope(node), local)
                if program is not None or key not in self._names:
                    self._names[key] = program

    def _set(self, target: ast.AST, at: ast.AST, program: int) -> bool:
        """Mark *target*, assigned at *at*, as a launcher alias; return whether it is new."""
        if isinstance(target, ast.Name):
            table, key = self._names, (self._scope(at), target.id)
        else:
            spelled = _dotted(target)
            if spelled is None:
                return False
            table, key = self._attributes, (self._owning_class(at), spelled)
        if table.get(key) is not None:
            return False
        table[key] = program
        return True

    def _name_program(self, name: str, at: ast.AST) -> int | None:
        """Resolve *name* used at *at* through its enclosing scopes, skipping class bodies."""
        scope = self._anchor(at)
        innermost = True
        while True:
            if innermost or not isinstance(scope, ast.ClassDef):
                key = (id(scope) if scope is not None else 0, name)
                if key in self._names:
                    return self._names[key]
            if scope is None:
                return None
            scope, innermost = self._anchor(scope), False

    def program_index(self, expr: ast.AST, at: ast.AST) -> int | None:
        """Return the program argument index if *expr* names a launcher at *at*."""
        if isinstance(expr, ast.Name):
            return self._name_program(expr.id, at)
        if not isinstance(expr, ast.Attribute):
            return None
        spelled = _dotted(expr)
        if spelled is not None:
            for key in ((self._owning_class(at), spelled), (0, spelled)):
                if key in self._attributes:
                    if self._attributes[key] is not None:
                        return self._attributes[key]
                    break
        owner, attr = _dotted(expr.value), expr.attr
        if owner in self._modules.subprocess and attr in _LAUNCHERS:
            return 0
        if owner in self._modules.asyncio and attr in _ASYNC_LAUNCHERS:
            return 0
        if owner in self._modules.os:
            return _os_program_index(attr)
        return None


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


def _launch_program(
    node: ast.Call, aliases: _Aliases, binders: frozenset[str]
) -> tuple[bool, ast.AST | None]:
    """Return whether *node* launches a program, and the argument naming it.

    A functools ``partial(<launcher>, ...)`` or ``partialmethod(...)`` counts
    as a launch with its bound arguments.
    """
    func, args = node.func, list(node.args)
    if args and _dotted(func) in binders:
        func, args = args[0], args[1:]
    program = aliases.program_index(func, node)
    if program is None:
        return False, None
    if len(args) > program:
        return True, args[program]
    if args:
        return True, None
    return True, next((kw.value for kw in node.keywords if kw.arg in _PROGRAM_KEYWORDS), None)


def _callee_name(func: ast.AST) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    return func.attr if isinstance(func, ast.Attribute) else None


def _forwarders(
    index: TreeIndex | None, nodes: tuple[ast.AST, ...], aliases: _Aliases, binders: frozenset[str]
) -> frozenset[str]:
    """Names of functions in the file that hand their ``executable`` or ``**kwargs`` to a launch.

    Followed through other such wrappers, so ``_run(argv, executable="git")``
    counts like the launch it forwards to.
    """
    candidates = []
    for node in nodes:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and index is not None:
            arguments = node.args
            forwarded = {
                arg.arg
                for arg in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)
                if arg.arg == "executable"
            }
            if arguments.kwarg is not None:
                forwarded.add(arguments.kwarg.arg)
            if forwarded:
                calls = [child for child in index.walk(node) if isinstance(child, ast.Call)]
                candidates.append((node.name, forwarded, calls))
    names: set[str] = set()
    changed = True
    while changed:
        changed = False
        for name, forwarded, calls in candidates:
            if name not in names and any(
                (_launch_program(call, aliases, binders)[0] or _callee_name(call.func) in names)
                and any(
                    keyword.arg in {"executable", None}
                    and isinstance(keyword.value, ast.Name)
                    and keyword.value.id in forwarded
                    for keyword in call.keywords
                )
                for call in calls
            ):
                names.add(name)
                changed = True
    return frozenset(names)


def _bare_launch_lines(index: TreeIndex | None, nodes: tuple[ast.AST, ...]) -> list[int]:
    """Lines launching a program by literal name, directly or through a local name."""
    modules = _modules(nodes)
    aliases = _Aliases(index, nodes, modules)
    forwarders = _forwarders(index, nodes, aliases, modules.binders)
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
        launches, program = _launch_program(node, aliases, modules.binders)
        if not launches and _callee_name(node.func) not in forwarders:
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


def _may_launch(facts: object, text: Callable[[], str | None]) -> bool:
    """Cheap fact prefilter: only files that can launch or look up a program get a tree scan.

    Call facts keep the written spelling, so any call whose final attribute is
    a launcher, ``which`` or a ``partial`` binder qualifies whatever its module
    alias; a ``from`` import of a launch module covers bare-name calls. A file
    importing only ``os`` qualifies when its text names an os launcher it may
    alias without calling it.
    """
    for call in getattr(facts, "calls", ()):
        attr = call.qualname.rpartition(".")[2]
        if attr in _LAUNCHERS or attr in _ASYNC_LAUNCHERS or attr in _BINDERS or attr == "which":
            return True
        if attr.startswith(("exec", "spawn", "posix_spawn")):
            return True
    # A plain ``import subprocess`` / ``import asyncio`` can feed a local
    # launcher alias whose calls carry no launcher fact. Nearly every module
    # imports ``os``, so those files are read for an os launcher name instead.
    imports = getattr(facts, "imports", ())
    if any(
        (fact.module is not None and fact.module.split(".")[0] in _LAUNCH_MODULES)
        or (
            fact.module is None
            and any(name.split(".")[0] in {"subprocess", "asyncio"} for name in fact.names)
        )
        for fact in imports
    ):
        return True
    if not any(fact.module is None and "os" in fact.names for fact in imports):
        return False
    source = text()
    return source is not None and _OS_LAUNCHER_REF.search(source) is not None


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
        if not _may_launch(facts, lambda path=path: provider.source_cache.read(path)[0]):
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
        for line in _bare_launch_lines(index, nodes):
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
