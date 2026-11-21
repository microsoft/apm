"""Equal-copy (D5) analysis over ``ast-body-v1`` duplicate groups.

A test T is an equal copy of a kept twin K only when all of these hold:

1. identical normalized body AST (``ast-body-v1``: docstrings stripped,
   literals kept);
2. identical decorators, including marks and parametrize values;
3. identical signature, with every fixture parameter resolving (class,
   module, then ``conftest.py`` chain) to an identical definition and
   scope, recursively;
4. identical class context: bases (resolved when defined in the module),
   class decorators and every non-test member of each enclosing class;
5. identical module ``pytestmark``, ``usefixtures`` and autouse fixtures
   (class, module and ``conftest.py``, resolved recursively), module
   setup/teardown, and identical bindings for every module-level name the
   body, its class or its fixtures reference (transitively). Code that
   depends on its file location (``__file__``, relative or star imports)
   never matches across files;
6. the same CI lane (same top-level test directory; markers are covered
   by 2, 4 and 5).

This module only reports; it never edits or deletes tests.
"""

from __future__ import annotations

import ast
import hashlib
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from test_body_hash import TestFunction, body_digest, iter_test_functions

_SETUP_NAMES = frozenset(
    {
        "setup_method",
        "teardown_method",
        "setup_class",
        "teardown_class",
        "setUp",
        "tearDown",
        "setUpClass",
        "tearDownClass",
        "setup",
        "teardown",
        "setup_module",
        "teardown_module",
        "setup_function",
        "teardown_function",
    }
)
_IMPLICIT_PARAMS = frozenset({"self", "cls"})
_LOCATION_NAMES = frozenset({"__file__", "__name__", "__package__", "__spec__"})


def _dump(node: ast.AST | Iterable[ast.AST] | None) -> str:
    if node is None:
        return ""
    if isinstance(node, ast.AST):
        return ast.dump(node, include_attributes=False)
    return "\n".join(ast.dump(item, include_attributes=False) for item in node)


def _is_fixture(node: ast.stmt) -> bool:
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Attribute) and target.attr == "fixture":
            return True
        if isinstance(target, ast.Name) and target.id == "fixture":
            return True
    return False


def _is_autouse(node: ast.stmt) -> bool:
    return _is_fixture(node) and any(
        isinstance(decorator, ast.Call)
        and any(
            keyword.arg == "autouse"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in decorator.keywords
        )
        for decorator in node.decorator_list  # type: ignore[attr-defined]
    )


def _fixture_name(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    for decorator in node.decorator_list:
        if isinstance(decorator, ast.Call):
            for keyword in decorator.keywords:
                if (
                    keyword.arg == "name"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ):
                    return keyword.value.value
    return node.name


def _params(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    arguments = node.args
    names = [arg.arg for arg in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)]
    return [name for name in names if name not in _IMPLICIT_PARAMS]


def _loaded_names(node: ast.AST) -> set[str]:
    return {
        child.id
        for child in ast.walk(node)
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load)
    }


def _bound_names(statement: ast.stmt) -> set[str]:
    if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {statement.name}
    if isinstance(statement, (ast.Import, ast.ImportFrom)):
        return {(alias.asname or alias.name).split(".", 1)[0] for alias in statement.names}
    targets: list[ast.expr] = []
    if isinstance(statement, ast.Assign):
        targets = list(statement.targets)
    elif isinstance(statement, (ast.AnnAssign, ast.AugAssign)):
        targets = [statement.target]
    return {
        child.id for target in targets for child in ast.walk(target) if isinstance(child, ast.Name)
    }


@dataclass
class _Module:
    path: str
    tree: ast.Module
    bindings: dict[str, list[ast.stmt]] = field(default_factory=dict)
    fixtures: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = field(default_factory=dict)
    star_import: bool = False

    def __post_init__(self) -> None:
        self.star_import = any(
            isinstance(statement, ast.ImportFrom)
            and any(alias.name == "*" for alias in statement.names)
            for statement in self.tree.body
        )
        for statement in self.tree.body:
            for name in _bound_names(statement):
                self.bindings.setdefault(name, []).append(statement)
            if _is_fixture(statement):
                self.fixtures[_fixture_name(statement)] = statement  # type: ignore[arg-type]

    def binding_closure(self, roots: Iterable[ast.AST]) -> str:
        """Dump every module-level binding the roots reference, transitively.

        Code that depends on where the file lives (``__file__``, ``__name__``,
        relative or star imports) is tied to the file path, so it never
        compares equal across files.
        """
        pending = set().union(*(_loaded_names(root) for root in roots))
        seen: set[str] = set()
        dumps: list[str] = []
        located = bool(pending & _LOCATION_NAMES)
        while pending:
            name = pending.pop()
            if name in seen or name not in self.bindings:
                continue
            seen.add(name)
            for statement in self.bindings[name]:
                dumps.append(f"{name}={_dump(statement)}")
                loaded = _loaded_names(statement)
                located |= bool(loaded & _LOCATION_NAMES) or (
                    isinstance(statement, ast.ImportFrom) and statement.level > 0
                )
                pending |= loaded - seen
        if located or self.star_import:
            dumps.append(f"<located:{self.path}>")
        return "\n".join(sorted(dumps))

    def context(self) -> str:
        """Dump module pytestmark, autouse fixtures and module setup/teardown."""
        parts = [_dump(statement) for statement in self.bindings.get("pytestmark", [])]
        for statement in self.tree.body:
            if _is_autouse(statement) or (
                isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
                and statement.name in _SETUP_NAMES
            ):
                parts.append(_dump(statement))
        return "\n".join(parts)


class CorpusIndex:
    """Parse test modules and conftest files once for D5 comparisons."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._modules: dict[str, _Module | None] = {}

    def module(self, relative: str) -> _Module | None:
        if relative not in self._modules:
            path = self.root / relative
            self._modules[relative] = (
                _Module(relative, ast.parse(path.read_text(encoding="utf-8"), filename=relative))
                if path.is_file()
                else None
            )
        return self._modules[relative]

    def _conftests(self, relative: str) -> list[_Module]:
        chain: list[_Module] = []
        parent = Path(relative).parent
        while parent.parts:
            module = self.module((parent / "conftest.py").as_posix())
            if module is not None:
                chain.append(module)
            parent = parent.parent
        return chain

    def _fixture_identity(
        self,
        name: str,
        function: TestFunction,
        seen: frozenset[str],
    ) -> str:
        if name in seen:
            return f"{name}=<cycle>"
        for cls in reversed(function.classes):
            for statement in cls.body:
                if _is_fixture(statement) and _fixture_name(statement) == name:  # type: ignore[arg-type]
                    return self._definition(
                        statement, self.module(function.path), function, seen | {name}
                    )  # type: ignore[arg-type]
        owner = self.module(function.path)
        candidates = [owner] if owner is not None else []
        candidates += self._conftests(function.path)
        for module in candidates:
            if name in module.fixtures:
                return self._definition(module.fixtures[name], module, function, seen | {name})
        return f"{name}=<external>"

    def _definition(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        module: _Module | None,
        function: TestFunction,
        seen: frozenset[str],
    ) -> str:
        parts = [_dump(node)]
        if module is not None:
            parts.append(module.binding_closure([node]))
        parts += [self._fixture_identity(param, function, seen) for param in _params(node)]
        return "\n".join(parts)

    def _autouse_identities(self, function: TestFunction) -> list[str]:
        module = self.module(function.path)
        sources: list[tuple[ast.stmt, _Module | None]] = [
            (statement, module) for cls in function.classes for statement in cls.body
        ]
        if module is not None:
            sources += [(statement, module) for statement in module.tree.body]
        for conftest in self._conftests(function.path):
            sources += [(statement, conftest) for statement in conftest.tree.body]
        return [
            self._definition(statement, owner, function, frozenset())  # type: ignore[arg-type]
            for statement, owner in sources
            if _is_autouse(statement)
        ]

    def equal_copy_key(self, function: TestFunction) -> str:
        """Return a digest that is equal only for D5-equal tests."""
        module = self.module(function.path)
        node = function.node
        marks: list[ast.AST] = [*node.decorator_list]
        for cls in function.classes:
            marks += cls.decorator_list
        if module is not None:
            marks += module.bindings.get("pytestmark", [])
        used = sorted(set(_params(node)) | _usefixtures(marks))
        class_context = [
            "\n".join(
                [
                    _dump(cls.bases),
                    _dump(cls.keywords),
                    _dump(cls.decorator_list),
                    *(
                        _dump(statement)
                        for statement in cls.body
                        if not (
                            isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
                            and statement.name.startswith("test")
                        )
                        and not (
                            isinstance(statement, ast.ClassDef)
                            and statement.name.startswith("Test")
                        )
                    ),
                ]
            )
            for cls in function.classes
        ]
        closure_roots: list[ast.AST] = [node, *marks]
        for cls in function.classes:
            closure_roots += [*cls.bases, *cls.keywords]
            closure_roots += [
                statement
                for statement in cls.body
                if not (
                    isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and statement.name.startswith("test")
                )
            ]
        parts = [
            body_digest(function),
            _dump(node.decorator_list),
            _dump(node.args),
            type(node).__name__,
            *(self._fixture_identity(name, function, frozenset()) for name in used),
            *class_context,
            *self._autouse_identities(function),
            module.context() if module is not None else "",
            module.binding_closure(closure_roots) if module is not None else "",
            _lane(function.path),
        ]
        return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


def _usefixtures(marks: Iterable[ast.AST]) -> set[str]:
    names: set[str] = set()
    for mark in marks:
        for node in ast.walk(mark):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "usefixtures"
            ):
                names |= {
                    arg.value
                    for arg in node.args
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                }
    return names


def _lane(path: str) -> str:
    parts = Path(path).parts
    return parts[1] if len(parts) > 2 else parts[0]


def equal_copy_groups(
    index: CorpusIndex,
    paths: Iterable[str],
) -> list[tuple[str, list[str]]]:
    """Return (kept test, deletable equal copies) for every D5 group."""
    by_body: dict[str, list[TestFunction]] = defaultdict(list)
    for path in paths:
        module = index.module(path)
        if module is None:
            continue
        functions = {
            function.nodeid: function for function in iter_test_functions(module.tree, path)
        }
        for function in functions.values():
            by_body[body_digest(function)].append(function)
    groups: list[tuple[str, list[str]]] = []
    for functions in by_body.values():
        if len(functions) < 2:
            continue
        by_key: dict[str, list[str]] = defaultdict(list)
        for function in functions:
            by_key[index.equal_copy_key(function)].append(function.nodeid)
        for ids in by_key.values():
            if len(ids) > 1:
                keep, *copies = sorted(ids)
                groups.append((keep, copies))
    return sorted(groups)
