#!/usr/bin/env python3
"""Detect local reimplementations of ``skill_subset_filter_tokens()``.

``skill_subset_filter_tokens()`` (src/apm_cli/models/dependency/subsets.py)
is the single canonical owner of skill subset match tokens: it normalizes
Windows-style separators, extracts the deployed leaf name, and collects the
raw/normalized/leaf token set. Skill selection consumers must call it rather
than re-derive the same tokens locally.

The lexical guard in scripts/lint-architecture-boundaries.sh only catches
the exact retired shape (``def _skill_subset_name_filter`` or a literal
``Path(normalized_path).name``). A renamed helper that reimplements the same
*algorithm* under a different name evades that grep. This script closes that
gap with a narrow AST check: it flags any function, in the specific consumer
files passed on the command line, whose own body (not counting nested
function/lambda scopes) combines all three signals of the duplicated
algorithm:

  1. slash normalization  -- ``X.replace("\\\\", "/")``
  2. path leaf extraction -- ``PurePosixPath(...).name`` (or ``Path``/
     ``PureWindowsPath``/``PurePath``)
  3. token-set collection -- ``tokens.add(...)``

A function that calls ``skill_subset_filter_tokens(...)`` directly is never
flagged, even if it also happens to touch one of the three signals.

This is intentionally NOT a general dataflow analyzer: it scans only the
files given on the command line (currently
``src/apm_cli/integration/skill_integrator.py`` and
``src/apm_cli/bundle/plugin_exporter.py``, wired from
scripts/lint-architecture-boundaries.sh) and looks for one specific
duplicated shape.
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

CANONICAL_OWNER = "skill_subset_filter_tokens"
CANONICAL_OWNER_MODULE = "src/apm_cli/models/dependency/subsets.py"

_LEAF_PATH_CALLEES = frozenset({"PurePosixPath", "PureWindowsPath", "PurePath", "Path"})


@dataclass(frozen=True)
class Violation:
    """A single function that reimplements the canonical owner's algorithm."""

    path: Path
    line: int
    qualname: str

    def render(self) -> str:
        """Return a one-line, actionable diagnostic for this violation."""
        return (
            f"{self.path}:{self.line}: function '{self.qualname}' reimplements the "
            f"{CANONICAL_OWNER}() normalization algorithm (slash normalization + "
            "path-leaf extraction + token-set collection) -- call "
            f"{CANONICAL_OWNER}() from {CANONICAL_OWNER_MODULE} instead of duplicating it."
        )


def _is_backslash_to_slash_replace(node: ast.AST) -> bool:
    """Return True for a call shaped like ``X.replace("\\\\", "/")``."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr == "replace"):
        return False
    if len(node.args) != 2:
        return False
    first, second = node.args
    if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
        return False
    if not (isinstance(second, ast.Constant) and isinstance(second.value, str)):
        return False
    return "\\" in first.value and second.value == "/"


def _is_path_leaf_access(node: ast.AST) -> bool:
    """Return True for an attribute access shaped like ``PurePosixPath(...).name``."""
    if not (isinstance(node, ast.Attribute) and node.attr == "name"):
        return False
    value = node.value
    if not isinstance(value, ast.Call):
        return False
    callee = value.func
    if isinstance(callee, ast.Name):
        return callee.id in _LEAF_PATH_CALLEES
    if isinstance(callee, ast.Attribute):
        return callee.attr in _LEAF_PATH_CALLEES
    return False


def _is_token_set_add(node: ast.AST) -> bool:
    """Return True for a call shaped like ``tokens.add(...)``."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return isinstance(func, ast.Attribute) and func.attr == "add"


def _calls_canonical_owner(node: ast.AST) -> bool:
    """Return True for a direct call to the canonical owner function."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == CANONICAL_OWNER
    if isinstance(func, ast.Attribute):
        return func.attr == CANONICAL_OWNER
    return False


def _walk_own_scope(node: ast.AST) -> Iterator[ast.AST]:
    """Yield descendants of ``node`` without descending into nested scopes.

    Nested ``def``/``async def``/``lambda`` bodies are excluded so a
    duplicate inside a helper does not get misattributed to its caller (and
    vice versa) -- each function is judged solely on its own statements.
    """
    for child in ast.iter_child_nodes(node):
        yield child
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            continue
        yield from _walk_own_scope(child)


def _build_qualnames(tree: ast.Module) -> dict[ast.AST, str]:
    """Map each function/method node in ``tree`` to a dotted qualified name."""
    qualnames: dict[ast.AST, str] = {}

    def walk(node: ast.AST, prefix: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                walk(child, [*prefix, child.name])
            elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                name = [*prefix, child.name]
                qualnames[child] = ".".join(name)
                walk(child, name)
            else:
                walk(child, prefix)

    walk(tree, [])
    return qualnames


def find_violations(paths: Sequence[Path]) -> list[Violation]:
    """Return every local skill-subset token normalizer found in ``paths``.

    Only files that exist and parse as Python are scanned; missing or
    unparseable files are silently skipped so this stays a narrow, targeted
    check rather than a general-purpose linter.
    """
    violations: list[Violation] = []
    for path in paths:
        if not path.is_file():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue

        for func, qualname in _build_qualnames(tree).items():
            scope_nodes = list(_walk_own_scope(func))
            if any(_calls_canonical_owner(n) for n in scope_nodes):
                continue
            has_replace = any(_is_backslash_to_slash_replace(n) for n in scope_nodes)
            has_leaf = any(_is_path_leaf_access(n) for n in scope_nodes)
            has_add = any(_is_token_set_add(n) for n in scope_nodes)
            if has_replace and has_leaf and has_add:
                violations.append(Violation(path=path, line=func.lineno, qualname=qualname))

    violations.sort(key=lambda v: (str(v.path), v.line))
    return violations


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parse command-line arguments for the checker."""
    parser = argparse.ArgumentParser(
        description=(
            "Detect local reimplementations of skill_subset_filter_tokens() "
            "in the given consumer source files."
        )
    )
    parser.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help="Consumer source files to scan (e.g. skill_integrator.py, plugin_exporter.py).",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the checker over the given paths and return a process exit code."""
    args = _parse_args(argv)
    violations = find_violations(args.paths)
    if violations:
        for violation in violations:
            print(f"[x] {violation.render()}")
        print(f"[x] {len(violations)} skill-subset owner violation(s) found")
        return 1
    print(f"[+] no skill-subset owner duplication found in {len(args.paths)} file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
