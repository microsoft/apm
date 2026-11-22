"""Closed-world lifetime of ``utils.path_security.CheckRoot`` (rt-06, #4802).

A ``CheckRoot`` is one containment check's live root: the project root and its
ancestors are trusted for that check only. Kept beyond the call it would turn
into a cross-check cache. This check is closed-world:

- Only the functions in ``_CONSTRUCTORS`` may build one.
- Inside any function, a name bound to a ``CheckRoot`` (built there, aliased,
  or a parameter annotated ``CheckRoot``) may only be passed to a receiver
  (``self.<name>`` or ``DeploymentLedgerCodec.<name>`` for a name in
  ``_RECEIVERS``), read through ``_READS``, have ``.resolve``/``.within``
  called on the spot, be compared, or be aliased to a plain local. Returning,
  yielding, storing it on an attribute, subscript or container, capturing it in
  a nested function, lambda or generator, or rebinding it with
  ``global``/``nonlocal`` is a violation.
- Receivers are the ledger's own methods: defining, assigning or importing a
  receiver name anywhere else, or rebinding ``DeploymentLedgerCodec``, is a
  violation.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable

from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import violation
from scripts.architecture_linter.models import Violation

_OWNER = "src/apm_cli/utils/path_security.py"
_CONSTRUCTORS = frozenset(
    {
        ("src/apm_cli/core/deployment_ledger.py", "_locator"),
        ("src/apm_cli/core/deployment_ledger.py", "_write_locator"),
        ("src/apm_cli/core/deployment_ledger.py", "locator_for_path"),
        ("src/apm_cli/install/deployed_paths.py", "_try_target"),
    }
)
_LEDGER = "src/apm_cli/core/deployment_ledger.py"
_RECEIVERS = frozenset({"_encode", "_locator", "locator_for_path"})
_RECEIVER_OWNERS = frozenset({"self", "DeploymentLedgerCodec"})
_READS = frozenset({"resolved", "root"})
_CALLS = frozenset({"resolve", "within"})
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
# Scopes whose body may run after the enclosing call returns.
_DEFERRED = (*_FUNCTIONS, ast.GeneratorExp, ast.ClassDef)


def _is_check_root_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
    return name == "CheckRoot"


def _is_check_root_value(value: ast.AST, names: set[str]) -> bool:
    """Whether *value* evaluates to a CheckRoot: a call, an alias or an ``or`` of them."""
    if isinstance(value, ast.BoolOp):
        return any(_is_check_root_value(part, names) for part in value.values)
    return _is_check_root_call(value) or (isinstance(value, ast.Name) and value.id in names)


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def _enclosing_function(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.AST | None:
    current = parents.get(node)
    while current is not None and not isinstance(current, _FUNCTIONS):
        current = parents.get(current)
    return current


def _own_scope(node: ast.AST, function: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    """Whether *node* runs in *function*'s own frame (not a nested deferred scope)."""
    current = parents.get(node)
    while current is not None and current is not function:
        if isinstance(current, _DEFERRED):
            return False
        current = parents.get(current)
    return current is function


def _tracked_names(function: ast.AST, parents: dict[ast.AST, ast.AST]) -> set[str]:
    names: set[str] = set()
    args = getattr(function, "args", None)
    for arg in (*getattr(args, "args", ()), *getattr(args, "kwonlyargs", ())):
        if arg.annotation is not None and "CheckRoot" in ast.unparse(arg.annotation):
            names.add(arg.arg)
    changed = True
    while changed:
        changed = False
        for node in ast.walk(function):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            if not _own_scope(node, function, parents):
                continue
            if not _is_check_root_value(node.value, names):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in names:
                    names.add(target.id)
                    changed = True
    return names


def _use_allowed(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    """Whether one load of a tracked name (or a CheckRoot call) stays in this call."""
    parent = parents.get(node)
    if isinstance(parent, ast.keyword):
        parent = parents.get(parent)
    if isinstance(parent, ast.Call) and node is not parent.func:
        func = parent.func
        return (
            isinstance(func, ast.Attribute)
            and func.attr in _RECEIVERS
            and isinstance(func.value, ast.Name)
            and func.value.id in _RECEIVER_OWNERS
        )
    if isinstance(parent, ast.Attribute):
        if parent.attr in _CALLS:
            # A bound method keeps the instance: it must be invoked on the spot.
            call = parents.get(parent)
            return isinstance(call, ast.Call) and call.func is parent
        return parent.attr in _READS
    if isinstance(parent, ast.Compare):
        return True
    if isinstance(parent, ast.BoolOp):
        return _use_allowed(parent, parents)
    if isinstance(parent, ast.Assign):
        return all(isinstance(target, ast.Name) for target in parent.targets)
    if isinstance(parent, ast.AnnAssign):
        return isinstance(parent.target, ast.Name)
    return False


def _receiver_violations(path: str, tree: ast.AST, parents) -> list[Violation]:
    """A receiver name resolves only to the ledger's own methods (closed world)."""
    found: list[Violation] = []
    message = "CheckRoot receivers are the ledger's own methods: do not shadow or rebind them"
    for node in ast.walk(tree):
        bad = False
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in _RECEIVERS:
            bad = path != _LEDGER or not isinstance(parents.get(node), ast.ClassDef)
        elif isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            bad = node.id in _RECEIVERS or node.id in _RECEIVER_OWNERS
        elif isinstance(node, ast.Attribute) and not isinstance(node.ctx, ast.Load):
            bad = node.attr in _RECEIVERS
        elif isinstance(node, ast.alias):
            bound = node.asname or node.name.split(".")[0]
            source = getattr(parents.get(node), "module", "") or ""
            bad = bound in _RECEIVERS or (
                bound == "DeploymentLedgerCodec"
                and (node.name != bound or not source.endswith("core.deployment_ledger"))
            )
        elif isinstance(node, ast.arg):
            bad = node.arg in _RECEIVERS or node.arg == "DeploymentLedgerCodec"
        if bad:
            found.append(violation(_RID, path, message, line=getattr(node, "lineno", 0)))
    return found


def _file_violations(path: str, tree: ast.AST) -> list[Violation]:
    parents = _parents(tree)
    found = _receiver_violations(path, tree, parents)
    message = "A CheckRoot lives for one containment check: build it only in an allowed function"
    for node in ast.walk(tree):
        if _is_check_root_call(node):
            owner = _enclosing_function(node, parents)
            allowed = owner is not None and (path, getattr(owner, "name", "")) in _CONSTRUCTORS
            if not allowed or not _use_allowed(node, parents):
                found.append(violation(_RID, path, message, line=node.lineno))
        if isinstance(node, _FUNCTIONS[:2]):
            names = _tracked_names(node, parents)
            if not names:
                continue
            for inner in ast.walk(node):
                if isinstance(inner, (ast.Global, ast.Nonlocal)) and names & set(inner.names):
                    found.append(
                        violation(_RID, path, "A CheckRoot must not be rebound", line=inner.lineno)
                    )
                elif (
                    isinstance(inner, ast.Name)
                    and inner.id in names
                    and not _own_scope(inner, node, parents)
                ):
                    found.append(
                        violation(
                            _RID,
                            path,
                            "A CheckRoot must not be captured by a nested function, "
                            "lambda or generator",
                            line=inner.lineno,
                        )
                    )
                elif (
                    isinstance(inner, ast.Name)
                    and inner.id in names
                    and isinstance(inner.ctx, ast.Load)
                    and not _use_allowed(inner, parents)
                ):
                    found.append(
                        violation(
                            _RID,
                            path,
                            "A CheckRoot must not escape its call (no return, yield, "
                            "container, attribute or subscript store)",
                            line=inner.lineno,
                        )
                    )
    return found


_RID = "transport-platform-url-path-security"


def check_root_lifetime(provider: FactsProvider, paths: Iterable[str]) -> tuple[Violation, ...]:
    """Closed-world CheckRoot lifetime over every source file that mentions it."""
    found: list[Violation] = []
    for path in paths:
        if path == _OWNER:
            continue
        lines, _error = provider.lexical_lines(path)
        if not any("CheckRoot" in line for line in lines):
            continue
        index = provider.tree_index(path)
        if index is not None and index.root is not None:
            found.extend(_file_violations(path, index.root))
    return tuple(found)
