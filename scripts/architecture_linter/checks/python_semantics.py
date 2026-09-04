"""Small AST queries shared by semantic architecture checks.

The linter's :class:`~scripts.architecture_linter.checks.tree_index.TreeIndex`
is the only traversal authority.  These helpers only filter its precomputed
node views; they never parse source or start a second AST walk.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from scripts.architecture_linter.checks.tree_index import (
    DEFINITION_NODES,
    FUNCTION_NODES,
    TreeIndex,
)


@dataclass(frozen=True)
class NameAssignment:
    """One assignment-like binding of a simple name."""

    node: ast.AST
    value: ast.AST | None


_ASSIGNMENT_NODES = (ast.Assign, ast.AnnAssign, ast.NamedExpr, ast.AugAssign)


def assignment_nodes(index: TreeIndex, scope: ast.AST) -> tuple[ast.AST, ...]:
    """Return every assignment-like node in a function's own scope."""
    return tuple(node for node in index.own_scope(scope) if isinstance(node, _ASSIGNMENT_NODES))


def direct_definitions(
    index: TreeIndex,
    name: str,
    *,
    parent: ast.AST | None = None,
    kinds: tuple[type[ast.AST], ...] = DEFINITION_NODES,
) -> tuple[ast.AST, ...]:
    """Return same-scope definitions named ``name`` in source order."""
    body = index.module_children() if parent is None else index.children(parent)
    return tuple(node for node in body if isinstance(node, kinds) and node.name == name)


def effective_definition(
    index: TreeIndex,
    name: str,
    *,
    parent: ast.AST | None = None,
    kinds: tuple[type[ast.AST], ...] = DEFINITION_NODES,
) -> ast.AST | None:
    """Return Python's last same-scope definition for ``name``."""
    definitions = direct_definitions(index, name, parent=parent, kinds=kinds)
    return definitions[-1] if definitions else None


def effective_function(
    index: TreeIndex,
    name: str,
    *,
    parent: ast.AST | None = None,
) -> ast.AST | None:
    """Return the effective direct function or method named ``name``."""
    return effective_definition(index, name, parent=parent, kinds=FUNCTION_NODES)


def scope_nodes(index: TreeIndex, scope: ast.AST | None) -> Sequence[ast.AST]:
    """Return nodes in one executable scope, excluding nested definition bodies."""
    if scope is not None:
        return index.own_scope(scope)
    return tuple(node for node in index.nodes if index.definition_anchor(node) is None)


def propagated_assignment_values(
    index: TreeIndex,
    resolver: Callable[[TreeIndex, ast.AST, dict[str, set[str]]], set[str]],
) -> dict[str, set[str]]:
    """Propagate resolver-derived values through simple name assignments."""
    assignments: list[tuple[list[str], ast.AST]] = []
    for node in index.walk(index.root):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names = [target.id for target in targets if isinstance(target, ast.Name)]
        if names:
            assignments.append((names, node.value))
    known: dict[str, set[str]] = {}
    for _ in range(len(assignments) + 1):
        changed = False
        for names, value in assignments:
            resolved = resolver(index, value, known)
            for name in names:
                if not resolved.issubset(known.get(name, set())):
                    known.setdefault(name, set()).update(resolved)
                    changed = True
        if not changed:
            break
    return known


def _target_binds_name(target: ast.AST, name: str) -> bool:
    if isinstance(target, ast.Name):
        return target.id == name
    if isinstance(target, (ast.Tuple, ast.List)):
        return any(_target_binds_name(item, name) for item in target.elts)
    if isinstance(target, ast.Starred):
        return _target_binds_name(target.value, name)
    return False


def assignments_to(
    index: TreeIndex,
    scope: ast.AST,
    name: str,
) -> tuple[NameAssignment, ...]:
    """Return every assignment to ``name`` in a function's own scope."""
    assignments: list[NameAssignment] = []
    for node in assignment_nodes(index, scope):
        binds = (
            isinstance(node, ast.Assign)
            and any(_target_binds_name(target, name) for target in node.targets)
        ) or (
            isinstance(node, (ast.AnnAssign, ast.NamedExpr, ast.AugAssign))
            and _target_binds_name(node.target, name)
        )
        if binds:
            assignments.append(NameAssignment(node, node.value))
    return tuple(assignments)


def import_bound_name(alias: ast.alias) -> str:
    """Return the local name bound by one import alias."""
    return alias.asname or alias.name.split(".", 1)[0]


def binding_nodes(
    index: TreeIndex,
    name: str,
    *,
    nodes: Iterable[ast.AST] | None = None,
) -> tuple[ast.AST, ...]:
    """Return non-canonical constructs that bind or delete ``name``.

    Import statements are included when an alias binds the requested name.
    Callers can remove one canonical import node by identity.
    """
    candidates = tuple(nodes) if nodes is not None else index.nodes
    found: list[ast.AST] = []
    for node in candidates:
        binds = (
            (
                isinstance(node, ast.Name)
                and node.id == name
                and isinstance(node.ctx, (ast.Store, ast.Del))
            )
            or (isinstance(node, ast.arg) and node.arg == name)
            or (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and node.name == name
            )
            or (isinstance(node, ast.ExceptHandler) and node.name == name)
            or (isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name == name)
            or (isinstance(node, ast.MatchMapping) and node.rest == name)
            or (isinstance(node, (ast.Global, ast.Nonlocal)) and name in node.names)
            or (
                isinstance(node, (ast.Import, ast.ImportFrom))
                and any(import_bound_name(alias) == name for alias in node.names)
            )
            or (
                isinstance(node, ast.Attribute)
                and isinstance(node.ctx, (ast.Store, ast.Del))
                and dotted_name(node).startswith(f"{name}.")
            )
        )
        if binds:
            found.append(node)
    return tuple(found)


def has_exclusive_import(
    index: TreeIndex,
    *,
    name: str,
    module: str,
    level: int,
    scope: ast.AST | None = None,
) -> bool:
    """Require one unaliased import to be the scope's only binding of ``name``."""
    nodes = scope_nodes(index, scope)
    imports = tuple(
        node
        for node in nodes
        if isinstance(node, ast.ImportFrom)
        and node.module == module
        and node.level == level
        and sum(alias.name == name and alias.asname is None for alias in node.names) == 1
    )
    return len(imports) == 1 and binding_nodes(index, name, nodes=nodes) == imports


def dotted_name(node: ast.AST | None) -> str:
    """Return a dotted ``Name``/``Attribute`` expression, or ``""``."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _constant_truth(node: ast.AST) -> bool | None:
    """Return literal truth when Python can decide it without execution."""
    if isinstance(node, ast.Constant):
        return bool(node.value)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return bool(node.elts)
    if isinstance(node, ast.Dict):
        return bool(node.keys)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        operand = _constant_truth(node.operand)
        return None if operand is None else not operand
    if isinstance(node, ast.BoolOp):
        values = tuple(_constant_truth(value) for value in node.values)
        if isinstance(node.op, ast.And):
            if False in values:
                return False
            return True if all(value is True for value in values) else None
        if True in values:
            return True
        return False if all(value is False for value in values) else None
    return None


def is_statically_dead(index: TreeIndex, node: ast.AST) -> bool:
    """Return whether ``node`` is nested under a constant-dead branch."""
    current = node
    while (parent := index.parent(current)) is not None:
        if isinstance(parent, (ast.If, ast.IfExp)):
            truth = _constant_truth(parent.test)
            body = parent.body if isinstance(parent, ast.If) else (parent.body,)
            orelse = parent.orelse if isinstance(parent, ast.If) else (parent.orelse,)
            if truth is False and current in body:
                return True
            if truth is True and current in orelse:
                return True
        elif isinstance(parent, ast.While):
            if _constant_truth(parent.test) is False and current in parent.body:
                return True
        elif isinstance(parent, ast.BoolOp) and current in parent.values:
            position = parent.values.index(current)
            prior_truth = tuple(_constant_truth(value) for value in parent.values[:position])
            if isinstance(parent.op, ast.And) and False in prior_truth:
                return True
            if isinstance(parent.op, ast.Or) and True in prior_truth:
                return True
        current = parent
    return False


__all__ = [
    "NameAssignment",
    "assignment_nodes",
    "assignments_to",
    "binding_nodes",
    "direct_definitions",
    "dotted_name",
    "effective_definition",
    "effective_function",
    "has_exclusive_import",
    "import_bound_name",
    "is_statically_dead",
    "propagated_assignment_values",
    "scope_nodes",
]
