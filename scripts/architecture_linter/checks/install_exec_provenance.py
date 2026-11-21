"""Closed-world provenance of executable allow-maps on deploy paths (#4018).

Every function in ``src/apm_cli`` that takes an executable allow-map (a
parameter named ``allow_executables`` or ``effective_allow_execs``) is a
consumer. Outside ``security/``, the map each consumer call receives must come
from the executable-trust-context owner: a producer call, a parameter passed
through, a local name bound only to producer calls, or a local provider whose
returns are producer calls. A literal, ``None``, the package manifest's own
grants or an omitted argument never reaches a consumer. Consumers are
discovered from the inventory, not listed, so a new deploy path is covered.
"""

from __future__ import annotations

import ast

from scripts.architecture_linter.checks.install_deployment_shared import (
    _facts_for,
    _lines,
    _python_paths,
    _summary,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.models import Violation

_SRC = "src/apm_cli/"
_SECURITY = "src/apm_cli/security/"
_MAP_PARAMS = ("allow_executables", "effective_allow_execs")
# Owner calls that produce an allow-map (security/executables.py, the install
# template wrapper, the local-bundle and audit-replay wrappers).
_PRODUCERS = frozenset(
    {
        "authorized_exec_map",
        "closed_exec_map",
        "effective_allow_map",
        "effective_bundle_allow_map",
        "effective_exec_map_for_project",
        "for_dependency",
        "materialize_exec_map",
        "resolve_project_exec_allow_map",
        # Returns (root, targets, provider of resolve_project_exec_allow_map).
        "scope_bundle_hook_inputs",
    }
)
# The deploy-path filter the trust owner itself exports; its other functions
# receive maps from the install adapters, which this check covers.
_SECURITY_CONSUMERS = frozenset({"filter_lsp_by_allow_executables"})
# Consumers whose map is not a deploy decision (the prompt's personal store).
_NOT_DEPLOY = frozenset({"prompt_executable_approval"})
_ALIASES: dict[str, str] = {}


def _callee(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return _ALIASES.get(func.id, func.id)
    return func.attr if isinstance(func, ast.Attribute) else None


def _set_aliases(tree: ast.AST) -> None:
    _ALIASES.clear()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.asname:
                    _ALIASES[alias.asname] = alias.name


def _params(function: ast.AST) -> list[str]:
    args = function.args  # type: ignore[attr-defined]
    return [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]


def _consumers(trees: dict[str, ast.AST]) -> dict[str, tuple[str, int | None]]:
    """Map each consumer name to ``(param, positional index or None)``."""
    found: dict[str, tuple[str, int | None]] = {}
    for path, tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if path.startswith(_SECURITY) and node.name not in _SECURITY_CONSUMERS:
                continue
            positional = [a.arg for a in (*node.args.posonlyargs, *node.args.args)]
            for param in _MAP_PARAMS:
                if param in positional:
                    index = positional.index(param)
                    if positional and positional[0] in ("self", "cls"):
                        index -= 1
                    found[node.name] = (param, index)
                elif param in [a.arg for a in node.args.kwonlyargs]:
                    found[node.name] = (param, None)
    return found


def _is_producer(value: ast.AST | None) -> bool:
    if isinstance(value, ast.Call):
        return _callee(value) in _PRODUCERS
    if isinstance(value, ast.IfExp):
        return _is_producer(value.body) and _is_producer(value.orelse)
    return False


def _bindings(scope: ast.AST, name: str) -> list[ast.AST | None]:
    """Values bound to *name* in *scope*, element-wise for tuple assignments."""
    values: list[ast.AST | None] = []
    for node in ast.walk(scope):
        if isinstance(node, ast.Assign):
            pairs = [(target, node.value) for target in node.targets]
        elif isinstance(node, ast.AnnAssign):
            pairs = [(node.target, node.value)]
        else:
            continue
        for target, value in pairs:
            if isinstance(target, ast.Name) and target.id == name:
                values.append(value)
            elif isinstance(target, ast.Tuple):
                for index, element in enumerate(target.elts):
                    if isinstance(element, ast.Name) and element.id == name:
                        if isinstance(value, ast.Tuple) and index < len(value.elts):
                            values.append(value.elts[index])
                        else:
                            # Unpacking one call: the call is the provenance.
                            values.append(value if isinstance(value, ast.Call) else None)
    return values


def _provider_returns_owner_map(scope: ast.AST, name: str) -> bool:
    for node in ast.walk(scope):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            returns = [item.value for item in ast.walk(node) if isinstance(item, ast.Return)]
            return bool(returns) and all(_is_producer(value) for value in returns)
    return False


def _owner_sourced(value: ast.AST, enclosing: ast.AST | None, module: ast.AST) -> bool:
    if _is_producer(value):
        return True
    if isinstance(value, ast.Lambda):
        return _is_producer(value.body)
    if not isinstance(value, ast.Name):
        return False
    scope = enclosing or module
    if enclosing is not None and value.id in _params(enclosing):
        return True
    if _provider_returns_owner_map(scope, value.id):
        return True
    bound = _bindings(scope, value.id)
    return bool(bound) and all(_is_producer(item) for item in bound)


def _bound_argument(call: ast.Call, param: str, index: int | None) -> ast.AST | str:
    for keyword in call.keywords:
        if keyword.arg == param:
            return keyword.value
        if keyword.arg is None:
            return "unknown"
    if index is not None and index < len(call.args):
        return call.args[index]
    return "omitted"


def _enclosing_functions(tree: ast.AST) -> dict[int, ast.AST]:
    """Map every node id to its innermost enclosing function."""
    owner: dict[int, ast.AST] = {}

    def visit(node: ast.AST, current: ast.AST | None) -> None:
        for child in ast.iter_child_nodes(node):
            inner = child if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else current
            if current is not None:
                owner[id(child)] = current
            visit(child, inner)

    visit(tree, None)
    return owner


def check_allow_map_provenance(provider: FactsProvider, rule_id: str) -> list[Violation]:
    """Every deploy-path consumer gets its allow-map from the trust owner."""
    trees: dict[str, ast.AST] = {}
    for path in _python_paths(provider, _SRC):
        facts, failures = _facts_for(provider, path, rule_id)
        if failures or not any(p in line for line in _lines(facts) for p in _MAP_PARAMS):
            continue
        index = provider.tree_index(path)
        if index is not None and index.root is not None:
            trees[path] = index.root
    consumers = _consumers(trees)
    names = tuple(consumers)
    for path in _python_paths(provider, _SRC):
        if path in trees or path.startswith(_SECURITY):
            continue
        facts, failures = _facts_for(provider, path, rule_id)
        if not failures and any(name + "(" in line for line in _lines(facts) for name in names):
            index = provider.tree_index(path)
            if index is not None and index.root is not None:
                trees[path] = index.root
    findings: list[Violation] = []
    for path, tree in trees.items():
        if path.startswith(_SECURITY):
            continue
        _set_aliases(tree)
        enclosing = _enclosing_functions(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _callee(node)
            if name not in consumers or name in _NOT_DEPLOY:
                continue
            param, position = consumers[name]
            value = _bound_argument(node, param, position)
            if value == "unknown":
                continue
            if value == "omitted" or not _owner_sourced(value, enclosing.get(id(node)), tree):
                shown = value if isinstance(value, str) else ast.unparse(value)[:60]
                findings.append(
                    _summary(
                        rule_id,
                        path,
                        f"{name} at line {node.lineno} must receive its {param} from the "
                        f"executable-trust-context owner, not {shown}",
                    )
                )
    return findings


__all__ = ["check_allow_map_provenance"]
