"""Keep rmtree error callbacks from changing a mode through a link."""

from __future__ import annotations

import ast

from scripts.architecture_linter.checks.transport_platform_shared import (
    GROUP,
    _load,
    _src_python,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import source_text, violation
from scripts.architecture_linter.models import Rule, Violation

_RULE_ID = "transport-platform-rmtree-callback-no-follow-chmod"
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


def _no_follow(call: ast.Call) -> bool:
    return any(
        keyword.arg == "follow_symlinks"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is False
        for keyword in call.keywords
    )


def _unsafe_chmod_lines(function: ast.AST) -> list[int]:
    """``os.chmod`` calls that may follow a link.

    A plain call is allowed only in the ``except NotImplementedError`` handler
    of a no-follow attempt (a platform without no-follow ``chmod``).
    """
    lines: list[int] = []

    def visit(node: ast.AST, in_fallback: bool) -> None:
        if _is_os_chmod(node) and not in_fallback and not _no_follow(node):
            lines.append(node.lineno)
        for child in ast.iter_child_nodes(node):
            fallback = in_fallback or (
                isinstance(child, ast.ExceptHandler)
                and isinstance(child.type, ast.Name)
                and child.type.id == "NotImplementedError"
            )
            visit(child, fallback)

    visit(function, False)
    return lines


def _passes_callback(text: str | None) -> bool:
    return bool(text) and "rmtree(" in text and ("onerror=" in text or "onexc=" in text)


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
        guard_ids=(_RULE_ID,),
        description="An rmtree error callback never changes a mode through a link.",
        check=_check_rmtree_callbacks,
    ),
)
COLLECTORS: tuple[object, ...] = ()
