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
     ``PureWindowsPath``/``PurePath``), including the split-statement form
     ``p = PurePosixPath(...)`` followed by ``leaf = p.name`` in the same
     function
  3. token-set collection -- ``tokens.add(...)``

Calling ``skill_subset_filter_tokens(...)`` directly does not exempt a
function: a function that both delegates to the canonical owner *and*
independently reimplements the three-signal algorithm is still flagged,
because the reimplementation is the actual duplication regardless of what
else the function does. A function is only clean if it does not combine all
three signals in its own scope -- which is naturally the case for a function
that does nothing but call the canonical owner.

This is intentionally NOT a general dataflow analyzer: it scans only the
files given on the command line (currently
``src/apm_cli/integration/skill_integrator.py`` and
``src/apm_cli/bundle/plugin_exporter.py``, wired from
scripts/lint-architecture-boundaries.sh) and looks for one specific
duplicated shape. The split-statement leaf-extraction tracking is limited to
the smallest pattern needed: simple ``name = LeafCallee(...)`` assignments
within the same function, matched against later ``name.name`` reads; it does
not follow assignments across function boundaries, reassignment shadowing,
or any other dataflow beyond that single hop.

A configured consumer path that is missing or not a regular file is a
misconfiguration, not "nothing to check": ``find_violations()`` fails closed
by raising ``ConfiguredPathError`` rather than silently skipping it, so the
guard cannot be evaded by pointing it at a path that no longer exists.
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


def _is_leaf_path_call(node: ast.AST) -> bool:
    """Return True for a call to one of the leaf-path constructors."""
    if not isinstance(node, ast.Call):
        return False
    callee = node.func
    if isinstance(callee, ast.Name):
        return callee.id in _LEAF_PATH_CALLEES
    if isinstance(callee, ast.Attribute):
        return callee.attr in _LEAF_PATH_CALLEES
    return False


def _leaf_path_call_targets(scope_nodes: Sequence[ast.AST]) -> set[str]:
    """Return local variable names assigned directly from a leaf-path call.

    Tracks the narrow split-statement pattern ``name = PurePosixPath(...)``
    (or ``Path``/``PureWindowsPath``/``PurePath``) so a later ``name.name``
    read in the same function is still recognized as leaf extraction even
    when split across two statements, e.g.::

        path = PurePosixPath(normalized)
        leaf = path.name

    This intentionally covers only simple ``Name = Call(...)`` assignments
    with exactly one target -- it is not general dataflow analysis, and does
    not attempt to track reassignment, tuple unpacking, or attribute/
    subscript targets.
    """
    leaf_vars: set[str] = set()
    for node in scope_nodes:
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and _is_leaf_path_call(node.value):
            leaf_vars.add(target.id)
    return leaf_vars


def _is_path_leaf_access(node: ast.AST, leaf_vars: frozenset[str] = frozenset()) -> bool:
    """Return True for a leaf-path ``.name`` read, direct or split across statements.

    Matches the direct form ``PurePosixPath(...).name`` as well as the split
    form where the leaf-path call was first assigned to a local variable
    (see ``_leaf_path_call_targets``) and ``.name`` is read off that
    variable in a later statement: ``leaf = path.name``.
    """
    if not (isinstance(node, ast.Attribute) and node.attr == "name"):
        return False
    value = node.value
    if _is_leaf_path_call(value):
        return True
    return isinstance(value, ast.Name) and value.id in leaf_vars


def _is_token_set_add(node: ast.AST) -> bool:
    """Return True for a call shaped like ``tokens.add(...)``."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return isinstance(func, ast.Attribute) and func.attr == "add"


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


class ConfiguredPathError(RuntimeError):
    """Raised when a configured consumer path is missing or not a regular file.

    A path passed to this checker is a hard-coded consumer file wired from
    scripts/lint-architecture-boundaries.sh, not user input: if it does not
    exist (or is a directory, socket, etc.), the checker cannot scan it and
    must fail closed rather than silently reporting "no violations", which
    would let a misconfigured or renamed path evade the guard entirely.
    """


def find_violations(paths: Sequence[Path]) -> list[Violation]:
    """Return every local skill-subset token normalizer found in ``paths``.

    Every path must exist and be a regular file; a missing or non-regular
    path raises ``ConfiguredPathError`` instead of being silently skipped
    (see ``ConfiguredPathError``). Files that exist but fail to parse as
    Python are still skipped, since a syntax error is a pre-existing problem
    unrelated to this narrow, targeted check.
    """
    violations: list[Violation] = []
    for path in paths:
        if not path.is_file():
            raise ConfiguredPathError(
                f"{path}: configured consumer path is missing or not a regular file"
            )
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue

        for func, qualname in _build_qualnames(tree).items():
            scope_nodes = list(_walk_own_scope(func))
            leaf_vars = _leaf_path_call_targets(scope_nodes)
            has_replace = any(_is_backslash_to_slash_replace(n) for n in scope_nodes)
            has_leaf = any(_is_path_leaf_access(n, leaf_vars) for n in scope_nodes)
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
    """Run the checker over the given paths and return a process exit code.

    Fails closed (nonzero, with an actionable diagnostic) if a configured
    consumer path is missing or not a regular file, rather than reporting a
    misleading "no violations found" for a path that was never scanned.
    """
    args = _parse_args(argv)
    try:
        violations = find_violations(args.paths)
    except ConfiguredPathError as exc:
        print(f"[x] {exc}")
        return 1
    if violations:
        for violation in violations:
            print(f"[x] {violation.render()}")
        print(f"[x] {len(violations)} skill-subset owner violation(s) found")
        return 1
    print(f"[+] no skill-subset owner duplication found in {len(args.paths)} file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
