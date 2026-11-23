"""Keep rmtree error callbacks from changing a mode through a link."""

from __future__ import annotations

import ast
import re

from scripts.architecture_linter.checks.transport_platform_shared import (
    GROUP,
    _load,
    _src_python,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import source_text, violation
from scripts.architecture_linter.models import Rule, Violation

_RULE_ID = "transport-platform-rmtree-callback-no-follow-chmod"
# The NotImplementedError fallback: it must follow a no-follow attempt and never follow.
_ATTEMPT_GUARD_ID = "transport-platform-rmtree-callback-fallback-attempt"
_FOLLOW_GUARD_ID = "transport-platform-rmtree-callback-fallback-follow"
_NESTED_GUARD_ID = "transport-platform-rmtree-callback-fallback-nested"
_OWNER = "src/apm_cli/utils/file_ops.py"
_CALLBACK_KEYWORDS = frozenset({"onerror", "onexc"})


def _callback_names(tree: ast.AST) -> set[str]:
    """Names of the functions a module passes to ``rmtree`` as its error callback."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        callee = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if callee != "rmtree":
            continue
        names.update(
            keyword.value.id
            for keyword in node.keywords
            if keyword.arg in _CALLBACK_KEYWORDS and isinstance(keyword.value, ast.Name)
        )
    return names


def _reached_functions(tree: ast.AST, roots: set[str]) -> list[ast.AST]:
    """The module's functions a callback reaches through same-module calls."""
    functions: dict[str, list[ast.AST]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            functions.setdefault(node.name, []).append(node)
    pending, seen = sorted(roots), set()
    reached: list[ast.AST] = []
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        for function in functions.get(name, []):
            reached.append(function)
            pending.extend(
                call.func.id
                for call in ast.walk(function)
                if isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id in functions
            )
    return reached


def _is_os_chmod(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "chmod"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "os"
    )


def _follow_symlinks(call: ast.Call) -> object:
    """The ``follow_symlinks`` keyword's literal value, or ``...`` when it is absent."""
    for keyword in call.keywords:
        if keyword.arg == "follow_symlinks":
            return keyword.value.value if isinstance(keyword.value, ast.Constant) else None
    return ...


def _no_follow_attempt(body: list[ast.stmt]) -> bool:
    """Whether *body* itself states the no-follow ``chmod`` its handler falls back from.

    Only a direct statement counts: an attempt nested in an inner ``try``,
    ``with`` or function may have its ``NotImplementedError`` consumed before
    it reaches this handler.
    """
    return any(
        isinstance(statement, ast.Expr)
        and _is_os_chmod(statement.value)
        and _follow_symlinks(statement.value) is False
        for statement in body
    )


def _unsafe_chmod_lines(function: ast.AST) -> list[int]:
    """``os.chmod`` calls that may follow a link.

    Only ``follow_symlinks=False`` is safe, except one plain call (no
    ``follow_symlinks``) in the ``except NotImplementedError`` handler of a
    ``try`` whose body attempts that no-follow ``chmod``: the fallback for a
    platform without it.
    """
    lines: list[int] = []

    def visit(node: ast.AST, in_fallback: bool) -> None:
        if _is_os_chmod(node):
            follow = _follow_symlinks(node)
            if follow is not False and not (in_fallback and follow is ...):
                lines.append(node.lineno)
        fallbacks: set[int] = set()
        if isinstance(node, ast.Try) and _no_follow_attempt(node.body):
            fallbacks = {
                id(handler)
                for handler in node.handlers
                if isinstance(handler.type, ast.Name) and handler.type.id == "NotImplementedError"
            }
        for child in ast.iter_child_nodes(node):
            visit(child, in_fallback or id(child) in fallbacks)

    visit(function, False)
    return lines


_CALLBACK_KEYWORD = re.compile(r"\bon(?:error|exc)\s*=")


def _passes_callback(text: str | None) -> bool:
    return bool(text) and "rmtree" in text and bool(_CALLBACK_KEYWORD.search(text))


def _check_rmtree_callbacks(provider: FactsProvider) -> tuple[Violation, ...]:
    """An rmtree error callback never changes a mode through a link."""
    inventory = frozenset(provider.inventory)
    findings: list[Violation] = []
    owner_callbacks = 0
    for path in _src_python(provider):
        lines, _error = provider.lexical_lines(path)
        if path != _OWNER and not _passes_callback("\n".join(lines)):
            continue
        facts, errors = _load(provider, inventory, _RULE_ID, path, parse=True)
        findings.extend(errors)
        if facts is None or not _passes_callback(source_text(facts)):
            continue
        index = provider.tree_index(path)
        tree = index.root if index is not None else None
        if tree is None:
            continue
        callbacks = _callback_names(tree)
        if path == _OWNER:
            owner_callbacks = len(callbacks)
        for function in _reached_functions(tree, callbacks):
            findings.extend(
                violation(
                    _RULE_ID,
                    path,
                    "An rmtree error callback must change a mode with "
                    "os.chmod(..., follow_symlinks=False), never through a link",
                    line=line,
                )
                for line in _unsafe_chmod_lines(function)
            )
    if not owner_callbacks:
        findings.append(
            violation(
                _RULE_ID,
                _OWNER,
                "robust_rmtree must pass a named error callback to shutil.rmtree",
            )
        )
    return tuple(findings)


RULES = (
    Rule(
        id=_RULE_ID,
        group=GROUP,
        guard_ids=(_RULE_ID, _ATTEMPT_GUARD_ID, _FOLLOW_GUARD_ID, _NESTED_GUARD_ID),
        description="An rmtree error callback never changes a mode through a link.",
        check=_check_rmtree_callbacks,
    ),
)
COLLECTORS: tuple[object, ...] = ()
