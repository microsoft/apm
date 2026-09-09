"""AST scan primitives for the Agent Plugin projection boundary analyzer.

Generic call/name/assignment recognition helpers plus the ``_Boundary``
context dataclass shared by every ``_check_*(ctx)`` boundary check. Part of
the facts-only port of
``scripts/check_agent_plugin_projection_boundary.py`` (legacy bundle-format
subcheck **B20**); see :mod:`agent_plugin_projection` for the entry point.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import dataclass

from scripts.architecture_linter.checks.tree_index import TreeIndex
from scripts.architecture_linter.groups.common import violation
from scripts.architecture_linter.models import Violation


def _call_name(node: ast.Call) -> str:
    """Dotted callee name, exactly as the legacy helper computed it."""
    parts: list[str] = []
    current: ast.expr = node.func
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def _function_calls(index: TreeIndex, node: ast.AST) -> set[str]:
    """Every dotted callee reachable from `node`."""
    return {_call_name(item) for item in index.walk(node) if isinstance(item, ast.Call)}


def _call_lines(index: TreeIndex, node: ast.AST) -> list[tuple[str, int]]:
    """Every ``(callee, line)`` pair reachable from `node`."""
    return [
        (_call_name(item), item.lineno) for item in index.walk(node) if isinstance(item, ast.Call)
    ]


def _functions(index: TreeIndex) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Every function or async-function definition anywhere in the file."""
    return list(index.functions())


def _named_functions(index: TreeIndex, name: str) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Every function definition named `name`, at any nesting depth."""
    return [node for node in _functions(index) if node.name == name]


def _module_functions(index: TreeIndex, name: str) -> list[ast.FunctionDef]:
    """Top-level ``def name`` statements only (the helper's ``tree.body`` filter)."""
    root = index.root
    if not isinstance(root, ast.Module):
        return []
    return [
        node
        for node in index.children(root)
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]


def _lines_for(names: Iterable[tuple[str, int]], wanted: Iterable[str]) -> list[int]:
    """Line numbers of calls whose dotted name is in `wanted`."""
    targets = frozenset(wanted)
    return [line for name, line in names if name in targets]


def _calls_public_configuration_thaw(index: TreeIndex, node: ast.AST) -> bool:
    """Whether `node` thaws ``configuration.values`` through the public helper."""
    for item in index.walk(node):
        if (
            isinstance(item, ast.Call)
            and isinstance(item.func, ast.Name)
            and item.func.id == "thaw_frozen_json"
            and len(item.args) == 1
            and isinstance(item.args[0], ast.Attribute)
            and item.args[0].attr == "values"
            and isinstance(item.args[0].value, ast.Name)
            and item.args[0].value.id == "configuration"
        ):
            return True
    return False


def _is_validation_package(node: ast.AST | None) -> bool:
    """Whether `node` is ``validation.package`` (optionally activated first)."""
    if (
        isinstance(node, ast.Call)
        and _call_name(node).endswith("._activate_validated_package")
        and node.args
    ):
        node = node.args[0]
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "package"
        and isinstance(node.value, ast.Name)
        and node.value.id == "validation"
    )


def _is_named_assignment(node: ast.AST, target: str, call: str) -> bool:
    """Whether `node` is ``target = call(...)`` with a single simple target."""
    return (
        isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == target
        and isinstance(node.value, ast.Call)
        and _call_name(node.value) == call
    )


def _stored_name_count(index: TreeIndex, node: ast.AST, name: str) -> int:
    """How many times `name` is bound anywhere inside `node`."""
    return sum(
        1
        for item in index.walk(node)
        if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store) and item.id == name
    )


def _first_executable_statement(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> ast.stmt | None:
    """First statement of `node`, skipping a leading docstring."""
    body = node.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return body[0] if body else None


def _is_call_statement(node: ast.AST | None, name: str) -> bool:
    """Whether `node` is a bare expression statement calling `name`."""
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and _call_name(node.value) == name
    )


def _is_native_package_predicate(node: ast.AST) -> bool:
    """Whether `node` tests ``... .package_type is not PackageType.AGENT_PLUGIN``."""
    return (
        isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Attribute)
        and node.left.attr == "package_type"
        and len(node.ops) == 1
        and isinstance(node.ops[0], ast.IsNot)
        and len(node.comparators) == 1
        and isinstance(node.comparators[0], ast.Attribute)
        and node.comparators[0].attr == "AGENT_PLUGIN"
        and isinstance(node.comparators[0].value, ast.Name)
        and node.comparators[0].value.id == "PackageType"
    )


def _raise_name(node: ast.Raise) -> str:
    """Dotted name of the exception a ``raise`` statement constructs."""
    if not isinstance(node.exc, ast.Call):
        return ""
    return _call_name(node.exc)


def _assigns_subscript_value(
    index: TreeIndex,
    node: ast.AST,
    *,
    owner: str,
    key: str,
    value: object,
) -> bool:
    """Whether `node` contains ``owner[key] = value`` with constant `value`."""
    return any(
        isinstance(item, ast.Assign)
        and len(item.targets) == 1
        and isinstance(item.targets[0], ast.Subscript)
        and isinstance(item.targets[0].value, ast.Name)
        and item.targets[0].value.id == owner
        and isinstance(item.targets[0].slice, ast.Constant)
        and item.targets[0].slice.value == key
        and isinstance(item.value, ast.Constant)
        and item.value.value == value
        for item in index.walk(node)
    )


def _bundle_format_attributes(index: TreeIndex, node: ast.AST) -> bool:
    """Whether ``BundleFormat.AGENT_PLUGIN`` appears anywhere inside `node`."""
    return any(
        isinstance(item, ast.Attribute)
        and item.attr == "AGENT_PLUGIN"
        and isinstance(item.value, ast.Name)
        and item.value.id == "BundleFormat"
        for item in index.walk(node)
    )


@dataclass(frozen=True)
class _Boundary:
    """Rule identity plus the rebuilt tree index of every owner file."""

    rule_id: str
    trees: dict[str, TreeIndex]

    def index(self, path: str) -> TreeIndex:
        """Return the pre-indexed tree for one owner file."""
        return self.trees[path]

    def report(self, path: str, message: str, line: int = 1) -> Violation:
        """Shape one boundary violation against its owner file."""
        return violation(self.rule_id, path, message, line=line)
