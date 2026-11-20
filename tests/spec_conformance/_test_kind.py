"""Closed classification of spec-conformance tests by kind.

A test linked to a requirement is BEHAVIORAL when it exercises APM itself, and
TEXT_SHAPE when it only asserts spec text or fixture shape. Only behavioral
tests count as implementation coverage in CONFORMANCE.{json,md}.

The rule is static and closed. Starting from the test function, follow every
name it references -- its own body, its fixture parameters, and transitively
every module-level function, class or assignment of the same module those
names resolve to. The test is BEHAVIORAL when that closure reaches:

* a name imported from a module under one of BEHAVIORAL_IMPORT_ROOTS, unless
  the module is one of NON_BEHAVIORAL_IMPORTS (parsing and constant helpers
  that read fixtures but run no APM decision); or
* a fixture parameter named in BEHAVIORAL_FIXTURES (the installed CLI).

Everything else is TEXT_SHAPE.
"""

from __future__ import annotations

import ast
import inspect
from functools import cache
from pathlib import Path

BEHAVIORAL = "behavioral"
TEXT_SHAPE = "text-shape"

BEHAVIORAL_IMPORT_ROOTS: tuple[str, ...] = (
    "apm_cli",
    "tests.integration",
    "tests.unit",
    "tests.utils",
)
NON_BEHAVIORAL_IMPORTS: frozenset[str] = frozenset(
    {
        "apm_cli.constants",
        "apm_cli.utils.yaml_io",
    }
)
BEHAVIORAL_FIXTURES: frozenset[str] = frozenset({"apm_binary_path"})


def _is_behavioral_module(module: str) -> bool:
    if module in NON_BEHAVIORAL_IMPORTS:
        return False
    return any(module == root or module.startswith(f"{root}.") for root in BEHAVIORAL_IMPORT_ROOTS)


def _import_bindings(nodes: list[ast.stmt] | list[ast.AST]) -> dict[str, str]:
    bindings: dict[str, str] = {}
    for node in nodes:
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                bindings[alias.asname or alias.name] = node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    bindings[alias.asname] = alias.name
                else:
                    root = alias.name.split(".")[0]
                    bindings[root] = alias.name
    return bindings


@cache
def _module_tree(path: str) -> ast.Module:
    return ast.parse(Path(path).read_text(encoding="utf-8"), filename=path)


@cache
def _module_index(path: str) -> tuple[dict[str, str], dict[str, tuple[ast.AST, ...]]]:
    tree = _module_tree(path)
    imports = _import_bindings(list(ast.walk(tree)))
    definitions: dict[str, list[ast.AST]] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            definitions.setdefault(node.name, []).append(node)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for name in ast.walk(target):
                    if isinstance(name, ast.Name):
                        definitions.setdefault(name.id, []).append(node)
    return imports, {name: tuple(nodes) for name, nodes in definitions.items()}


def _reachable_parts(node: ast.AST) -> list[ast.AST]:
    """Return the sub-nodes a reference can come from (a def's body, not its decorators)."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return list(node.body)
    return [node]


def _referenced_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for part in _reachable_parts(node):
        names.update(item.id for item in ast.walk(part) if isinstance(item, ast.Name))
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        names.update(arg.arg for arg in node.args.args + node.args.kwonlyargs)
    return names


def _local_import_modules(node: ast.AST) -> set[str]:
    modules: set[str] = set()
    for part in _reachable_parts(node):
        modules.update(_import_bindings(list(ast.walk(part))).values())
    return modules


def classify_definition(path: str, function: ast.AST) -> str:
    """Return BEHAVIORAL or TEXT_SHAPE for one test function node in *path*."""
    imports, definitions = _module_index(path)
    if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
        parameters = {arg.arg for arg in function.args.args + function.args.kwonlyargs}
        if parameters & BEHAVIORAL_FIXTURES:
            return BEHAVIORAL
    pending: list[ast.AST] = [function]
    seen_nodes: set[int] = set()
    seen_names: set[str] = set()
    while pending:
        node = pending.pop()
        if id(node) in seen_nodes:
            continue
        seen_nodes.add(id(node))
        if any(_is_behavioral_module(module) for module in _local_import_modules(node)):
            return BEHAVIORAL
        for name in _referenced_names(node) - seen_names:
            seen_names.add(name)
            module = imports.get(name)
            if module is not None and _is_behavioral_module(module):
                return BEHAVIORAL
            pending.extend(definitions.get(name, ()))
    return TEXT_SHAPE


def classify_function(function: object) -> str:
    """Return the kind of a collected pytest test function object.

    A test whose source cannot be located is TEXT_SHAPE: unknown coverage is
    never counted as implementation coverage.
    """
    target = inspect.unwrap(function)
    code = getattr(target, "__code__", None)
    try:
        path = inspect.getsourcefile(target)
    except TypeError:
        path = None
    if path is None or code is None:
        return TEXT_SHAPE
    _module_index(path)
    tree = _module_tree(path)
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == code.co_name
            and min([node.lineno, *(item.lineno for item in node.decorator_list)])
            <= code.co_firstlineno
            <= (node.end_lineno or node.lineno)
        ):
            return classify_definition(path, node)
    return TEXT_SHAPE
