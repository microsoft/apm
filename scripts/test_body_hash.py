"""Per-test normalized-AST body hashing shared by test-corpus ratchets.

Algorithm ``ast-body-v1``: for every pytest-collectable test function
(module-level ``test*`` functions and ``test*`` methods of ``Test*``
classes, including nested classes), drop a leading docstring, dump each
remaining body statement with ``ast.dump(..., include_attributes=False)``
and hash the joined dump with SHA-256 (first 16 hex characters kept).
Literals, names and call shapes are kept; comments, formatting, line
numbers and docstrings are ignored.

The body hash alone ignores decorators (marks, parametrize values),
the signature and fixture definitions, class context (bases, setup,
autouse fixtures) and module-level bindings. Equal hashes therefore
prove equal body text, not equal behavior. Callers that need behavioral
equality must compare those contexts as well; ``TestFunction`` keeps the
AST nodes they need.
"""

from __future__ import annotations

import ast
import hashlib
from collections.abc import Iterator
from dataclasses import dataclass

ALGORITHM = "ast-body-v1"
DIGEST_LENGTH = 16


@dataclass(frozen=True)
class TestFunction:
    """One collectable test function with its enclosing class chain."""

    __test__ = False

    path: str
    classes: tuple[ast.ClassDef, ...]
    node: ast.FunctionDef | ast.AsyncFunctionDef

    @property
    def qualname(self) -> str:
        """Return ``Class::Nested::name`` (or ``name`` at module level)."""
        return "::".join([*(cls.name for cls in self.classes), self.node.name])

    @property
    def nodeid(self) -> str:
        """Return the pytest-style node id without parametrize suffixes."""
        return f"{self.path}::{self.qualname}"


def _is_test_class(node: ast.stmt) -> bool:
    return isinstance(node, ast.ClassDef) and node.name.startswith("Test")


def _is_test_function(node: ast.stmt) -> bool:
    return isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
        "test"
    )


def _walk(
    path: str,
    body: list[ast.stmt],
    classes: tuple[ast.ClassDef, ...],
) -> Iterator[TestFunction]:
    for statement in body:
        if _is_test_function(statement):
            yield TestFunction(path=path, classes=classes, node=statement)  # type: ignore[arg-type]
        elif _is_test_class(statement):
            yield from _walk(path, statement.body, (*classes, statement))  # type: ignore[attr-defined]


def iter_test_functions(tree: ast.Module, path: str) -> Iterator[TestFunction]:
    """Yield collectable test functions of one parsed module in source order."""
    yield from _walk(path, tree.body, ())


def _strip_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[1:]
    return body


def body_digest(function: TestFunction) -> str:
    """Return the ``ast-body-v1`` digest of one test function body."""
    dumped = "\n".join(
        ast.dump(statement, include_attributes=False)
        for statement in _strip_docstring(function.node.body)
    )
    return hashlib.sha256(dumped.encode("utf-8")).hexdigest()[:DIGEST_LENGTH]
