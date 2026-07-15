#!/usr/bin/env python3
"""Enforce one semantic owner for Git repository cache identity.

The shell architecture gate previously checked AC10 with string greps. Those
checks caught the exact retired variable names but not equivalent truncation
hidden behind a renamed helper or applied after canonical normalization.

This checker validates the load-bearing AST shapes instead:

* ``SharedCloneCache.get_or_clone`` assigns ``repository`` exactly once from
  ``normalize_repo_url(repository_url)`` and keys entries directly by that
  value plus ``ref``.
* ``_repository_cache_identity`` returns the direct composition
  ``normalize_repo_url(dep_ref.to_github_url())`` without an intermediate
  transformation.
* L0 lookup, resolver coalescing, and lockfile seeding consume that helper
  directly.

The check is intentionally narrow. It guards two canonical consumer modules,
not arbitrary repository code, and fails closed on missing files, syntax
errors, methods, or expected owner expressions. Behavioral coverage remains
the second guardrail for normalizer internals and bare materialization:
``test_nested_gitlab_repositories_with_same_group_install_independently`` and
the req-rs-016 tests must stay paired with this structural check.
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

SHARED_CACHE_PATH = Path("src/apm_cli/deps/shared_clone_cache.py")
TIERED_RESOLVER_PATH = Path("src/apm_cli/deps/tiered_ref_resolver.py")


@dataclass(frozen=True)
class Violation:
    """One semantic repository-cache owner violation."""

    path: Path
    line: int
    message: str

    def render(self) -> str:
        """Return one actionable diagnostic."""
        return f"{self.path}:{self.line}: {self.message}"


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _call_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return None


def _is_name(node: ast.AST, name: str) -> bool:
    return isinstance(node, ast.Name) and node.id == name


def _is_call(node: ast.AST, name: str, args: tuple[str, ...]) -> bool:
    return (
        isinstance(node, ast.Call)
        and _call_name(node.func) == name
        and len(node.args) == len(args)
        and not node.keywords
        and all(
            _is_name(argument, expected) for argument, expected in zip(node.args, args, strict=True)
        )
    )


def _find_function(
    tree: ast.Module,
    function_name: str,
    *,
    class_name: str | None = None,
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    body: list[ast.stmt] = tree.body
    if class_name is not None:
        owner = next(
            (
                node
                for node in tree.body
                if isinstance(node, ast.ClassDef) and node.name == class_name
            ),
            None,
        )
        if owner is None:
            return None
        body = owner.body
    return next(
        (
            node
            for node in body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name == function_name
        ),
        None,
    )


def _assigned_values(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    target_name: str,
) -> list[ast.AST]:
    values: list[ast.AST] = []
    for node in ast.walk(function):
        if isinstance(node, ast.Assign) and any(
            _is_name(target, target_name) for target in node.targets
        ):
            values.append(node.value)
        elif isinstance(node, ast.AnnAssign) and _is_name(node.target, target_name):
            if node.value is not None:
                values.append(node.value)
        elif isinstance(node, ast.NamedExpr) and _is_name(node.target, target_name):
            values.append(node.value)
    return values


def _repository_ref_tuple(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Tuple)
        and len(node.elts) == 2
        and _is_name(node.elts[0], "repository")
        and _is_name(node.elts[1], "ref")
    )


def _repository_identity_call(node: ast.AST) -> bool:
    return _is_call(node, "_repository_cache_identity", ("dep_ref",))


def _identity_ref_tuple(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Tuple)
        and len(node.elts) == 2
        and _repository_identity_call(node.elts[0])
        and _is_name(node.elts[1], "ref")
    )


def _direct_identity_composition(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call) or _call_name(node.func) != "normalize_repo_url":
        return False
    if len(node.args) != 1 or node.keywords:
        return False
    url_call = node.args[0]
    return (
        isinstance(url_call, ast.Call)
        and _call_name(url_call.func) == "dep_ref.to_github_url"
        and not url_call.args
        and not url_call.keywords
    )


def _parse(source: str, path: Path) -> tuple[ast.Module | None, list[Violation]]:
    try:
        return ast.parse(source, filename=str(path)), []
    except SyntaxError as exc:
        return None, [
            Violation(
                path=path,
                line=exc.lineno or 1,
                message="cannot parse configured cache-identity owner source",
            )
        ]


def _analyze_shared(source: str, path: Path) -> list[Violation]:
    tree, violations = _parse(source, path)
    if tree is None:
        return violations
    method = _find_function(tree, "get_or_clone", class_name="SharedCloneCache")
    if method is None:
        return [Violation(path, 1, "SharedCloneCache.get_or_clone is missing")]

    repository_values = _assigned_values(method, "repository")
    if len(repository_values) != 1 or not _is_call(
        repository_values[0],
        "normalize_repo_url",
        ("repository_url",),
    ):
        violations.append(
            Violation(
                path,
                method.lineno,
                "get_or_clone must assign repository exactly once from "
                "normalize_repo_url(repository_url) without post-normalization transforms",
            )
        )

    key_values = _assigned_values(method, "key")
    if len(key_values) != 1 or not _repository_ref_tuple(key_values[0]):
        violations.append(
            Violation(
                path,
                method.lineno,
                "get_or_clone cache key must be the direct (repository, ref) tuple",
            )
        )

    bare_lookups = [
        node
        for node in ast.walk(method)
        if isinstance(node, ast.Call) and _call_name(node.func) == "self._find_repo_bare"
    ]
    if len(bare_lookups) != 1 or not (
        len(bare_lookups[0].args) == 1 and _is_name(bare_lookups[0].args[0], "repository")
    ):
        violations.append(
            Violation(
                path,
                method.lineno,
                "Tier-0 bare lookup must consume the direct normalized repository identity",
            )
        )
    return violations


def _analyze_tiered(source: str, path: Path) -> list[Violation]:
    tree, violations = _parse(source, path)
    if tree is None:
        return violations

    identity = _find_function(tree, "_repository_cache_identity")
    if identity is None:
        return [Violation(path, 1, "_repository_cache_identity is missing")]
    returns = [node for node in ast.walk(identity) if isinstance(node, ast.Return)]
    if (
        len(returns) != 1
        or returns[0].value is None
        or not _direct_identity_composition(returns[0].value)
        or any(
            isinstance(node, ast.Assign | ast.AnnAssign | ast.NamedExpr)
            for node in ast.walk(identity)
        )
    ):
        violations.append(
            Violation(
                path,
                identity.lineno,
                "_repository_cache_identity must directly return "
                "normalize_repo_url(dep_ref.to_github_url()) without indirect truncation",
            )
        )

    l0 = _find_function(tree, "try_resolve", class_name="L0PerRunCache")
    if l0 is None:
        violations.append(Violation(path, 1, "L0PerRunCache.try_resolve is missing"))
    else:
        returns = [node for node in ast.walk(l0) if isinstance(node, ast.Return)]
        valid_l0 = any(
            isinstance(node.value, ast.Call)
            and _call_name(node.value.func) == "self.cache.get"
            and len(node.value.args) == 2
            and _repository_identity_call(node.value.args[0])
            and _is_name(node.value.args[1], "ref")
            for node in returns
        )
        if not valid_l0:
            violations.append(
                Violation(
                    path,
                    l0.lineno,
                    "L0 lookup must call cache.get(_repository_cache_identity(dep_ref), ref)",
                )
            )

    resolve = _find_function(tree, "resolve", class_name="TieredRefResolver")
    if resolve is None:
        violations.append(Violation(path, 1, "TieredRefResolver.resolve is missing"))
    else:
        key_values = _assigned_values(resolve, "key")
        if len(key_values) != 1 or not _identity_ref_tuple(key_values[0]):
            violations.append(
                Violation(
                    path,
                    resolve.lineno,
                    "resolver coalescing key must be the direct "
                    "(_repository_cache_identity(dep_ref), ref) tuple",
                )
            )

    seed = _find_function(tree, "seed", class_name="TieredRefResolver")
    if seed is None:
        violations.append(Violation(path, 1, "TieredRefResolver.seed is missing"))
    else:
        puts = [
            node
            for node in ast.walk(seed)
            if isinstance(node, ast.Call) and _call_name(node.func) == "self._cache.put"
        ]
        if len(puts) != 1 or not (
            len(puts[0].args) >= 2
            and _repository_identity_call(puts[0].args[0])
            and _is_name(puts[0].args[1], "ref")
        ):
            violations.append(
                Violation(
                    path,
                    seed.lineno,
                    "lockfile seed must call _cache.put("
                    "_repository_cache_identity(dep_ref), ref, sha)",
                )
            )
    return violations


def analyze_sources(shared_source: str, tiered_source: str) -> list[Violation]:
    """Return all semantic owner violations in the configured source pair."""
    return sorted(
        [
            *_analyze_shared(shared_source, SHARED_CACHE_PATH),
            *_analyze_tiered(tiered_source, TIERED_RESOLVER_PATH),
        ],
        key=lambda violation: (str(violation.path), violation.line, violation.message),
    )


def check(root: Path) -> list[Violation]:
    """Read and analyze the canonical repository-cache consumer modules."""
    paths = (root / SHARED_CACHE_PATH, root / TIERED_RESOLVER_PATH)
    missing = [
        Violation(
            path.relative_to(root),
            1,
            "configured cache-identity owner path is missing or not a regular file",
        )
        for path in paths
        if not path.is_file()
    ]
    if missing:
        return missing
    return analyze_sources(
        paths[0].read_text(encoding="utf-8"),
        paths[1].read_text(encoding="utf-8"),
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check canonical Git repository cache identity consumers."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repository root.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the checker and return a process exit code."""
    args = _parse_args(argv)
    violations = check(args.root.resolve())
    for violation in violations:
        print(f"[x] {violation.render()}")
    if violations:
        print(f"[x] {len(violations)} repository cache identity owner violation(s) found")
        return 1
    print("[+] repository cache identity owner check clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
