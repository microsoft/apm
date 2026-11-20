#!/usr/bin/env python3
"""Prevent growth in per-test duplicate bodies and cross-file test names."""

from __future__ import annotations

import argparse
import ast
import hashlib
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from ratchet_baseline import (
    BaselineError,
    load_baseline,
    validate_provisional,
    write_baseline,
)
from test_file_inventory import tracked_python_paths

ALGORITHM = "ast-body-v1"
ALGORITHM_DESCRIPTION = (
    "Each pytest-collectable test (module-level 'test*' functions and 'test*' "
    "methods of 'Test*' or '*TestCase' classes, nested classes included) is "
    "fingerprinted as SHA-256 over ast.dump() of its decorators, arguments, "
    "return annotation and body with a leading docstring removed. Test names, "
    "docstrings, comments, formatting and source positions are ignored. Tests "
    "that share a fingerprint form one body duplicate group, matched by member "
    "test ids. Separately, one 'Class::test' qualified name defined in more than "
    "one file forms a name collision group. Groups may only shrink."
)
SCHEMA_VERSION = 2
SCOPE = ["tests/**/test_*.py", "tests/**/*_test.py"]
DEFAULT_BASELINE = Path("tests") / "quality" / "exact_test_duplicates.json"
UPDATE_COMMAND = "uv run --frozen python scripts/check_exact_test_duplicates.py --update-baseline"
BASELINE_KEYS = frozenset(
    {
        "algorithm",
        "algorithm_description",
        "body_duplicate_groups",
        "name_collision_groups",
        "schema_version",
        "scope",
    }
)


@dataclass(frozen=True)
class ScanResult:
    """Per-test duplicate observations for one repository tree."""

    test_count: int
    body_groups: dict[str, set[str]]
    name_groups: dict[str, set[str]]


def test_modules(root: Path, scope: list[str]) -> list[Path]:
    """Return contained, non-symlink tracked test modules for the scope."""
    return tracked_python_paths(root, scope=tuple(scope))


def _is_test_class(node: ast.ClassDef) -> bool:
    """Return whether pytest would collect tests from this class."""
    if node.name.startswith("Test"):
        return True
    for base in node.bases:
        name = base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", "")
        if name.endswith("TestCase"):
            return True
    return False


def iter_tests(
    body: list[ast.stmt],
    prefix: tuple[str, ...] = (),
) -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """Return (qualified name, node) for every collectable test in `body`."""
    found: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = []
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test"):
                found.append(("::".join((*prefix, node.name)), node))
        elif isinstance(node, ast.ClassDef) and _is_test_class(node):
            found.extend(iter_tests(node.body, (*prefix, node.name)))
    return found


def body_fingerprint(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Return the ast-body-v1 digest of one test function."""
    statements = list(node.body)
    first = statements[0] if statements else None
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        statements = statements[1:]
    parts = [type(node).__name__]
    parts.extend(ast.dump(decorator) for decorator in node.decorator_list)
    parts.append(ast.dump(node.args))
    parts.append(ast.dump(node.returns) if node.returns is not None else "")
    parts.extend(ast.dump(statement) for statement in statements)
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def scan(root: Path, modules: list[Path]) -> ScanResult:
    """Fingerprint every test in `modules` and group duplicates."""
    by_digest: dict[str, set[str]] = defaultdict(set)
    by_name: dict[str, set[str]] = defaultdict(set)
    test_count = 0
    for path in modules:
        relative = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_bytes(), filename=relative)
        for qualname, node in iter_tests(tree.body):
            test_count += 1
            by_digest[body_fingerprint(node)].add(f"{relative}::{qualname}")
            if "::" in qualname:
                by_name[qualname].add(relative)
    return ScanResult(
        test_count=test_count,
        body_groups={key: ids for key, ids in by_digest.items() if len(ids) > 1},
        name_groups={key: paths for key, paths in by_name.items() if len(paths) > 1},
    )


def _listed(members: set[str], new: set[str] = frozenset()) -> str:
    """Render one indented member list, flagging new members."""
    return "\n".join(
        f"  - {member}{' (new)' if member in new else ''}" for member in sorted(members)
    )


def compare_bodies(
    observed: dict[str, set[str]],
    allowed: list[frozenset[str]],
) -> tuple[list[str], list[str]]:
    """Return growth and reduction diagnostics for body duplicate groups."""
    owner = {test_id: group for group in allowed for test_id in group}
    growth: list[str] = []
    for digest, members in sorted(observed.items(), key=lambda item: sorted(item[1])):
        ceilings = {owner.get(test_id) for test_id in members}
        if len(ceilings) == 1 and None not in ceilings:
            continue
        new = {test_id for test_id in members if test_id not in owner}
        growth.append(
            f"duplicate test body {ALGORITHM}:{digest[:12]} is new or grew:\n"
            f"{_listed(members, new)}\n"
            "  Fix: delete the redundant copy, or parametrize or change it so each "
            "test checks distinct behavior; do not expand the baseline."
        )
    current = {frozenset(members) for members in observed.values()}
    stale = [
        f"body duplicate group reduced:\n{_listed(set(group))}"
        for group in sorted(allowed, key=sorted)
        if group not in current
    ]
    return growth, stale


def compare_names(
    observed: dict[str, set[str]],
    allowed: dict[str, set[str]],
) -> tuple[list[str], list[str]]:
    """Return growth and reduction diagnostics for cross-file name groups."""
    growth: list[str] = []
    for name, paths in sorted(observed.items()):
        ceiling = allowed.get(name, set())
        if paths <= ceiling:
            continue
        growth.append(
            f"test name {name} is defined in more than one file:\n"
            f"{_listed(paths, paths - ceiling)}\n"
            "  Fix: rename the new class or test so its Class::test name is unique "
            "across files, or remove the copied test; do not expand the baseline."
        )
    stale = [
        f"name collision group {name} reduced: {sorted(paths)} -> "
        f"{sorted(observed.get(name, set()))}"
        for name, paths in sorted(allowed.items())
        if observed.get(name, set()) != paths
    ]
    return growth, stale


def _sorted_unique_strings(value: object, minimum: int) -> bool:
    """Return whether `value` is a sorted, unique list of non-empty strings."""
    return (
        isinstance(value, list)
        and len(value) >= minimum
        and all(isinstance(item, str) and item for item in value)
        and value == sorted(set(value))
    )


def parse_baseline(
    payload: dict[str, object],
) -> tuple[list[frozenset[str]], dict[str, set[str]]]:
    """Validate a schema-2 baseline and return its allowed groups."""
    if not set(payload) >= BASELINE_KEYS or not set(payload) <= BASELINE_KEYS | {"provisional"}:
        raise BaselineError("invalid exact-duplicate baseline: unexpected object shape")
    if payload["schema_version"] != SCHEMA_VERSION or payload["algorithm"] != ALGORITHM:
        raise BaselineError("invalid exact-duplicate baseline: unsupported schema or algorithm")
    if payload["algorithm_description"] != ALGORITHM_DESCRIPTION:
        raise BaselineError(
            "invalid exact-duplicate baseline: algorithm_description must match "
            "ALGORITHM_DESCRIPTION in scripts/check_exact_test_duplicates.py"
        )
    if payload["scope"] != SCOPE:
        raise BaselineError("invalid exact-duplicate baseline: scope must remain exact")

    raw_bodies = payload["body_duplicate_groups"]
    if not isinstance(raw_bodies, list):
        raise BaselineError(
            "invalid exact-duplicate baseline: body_duplicate_groups must be a list"
        )
    bodies: list[frozenset[str]] = []
    seen_ids: set[str] = set()
    for group in raw_bodies:
        if not _sorted_unique_strings(group, 2):
            raise BaselineError(
                "invalid exact-duplicate baseline: body group ids must be sorted and unique"
            )
        if seen_ids.intersection(group):
            raise BaselineError("invalid exact-duplicate baseline: repeated test id")
        seen_ids.update(group)
        bodies.append(frozenset(group))

    raw_names = payload["name_collision_groups"]
    if not isinstance(raw_names, list):
        raise BaselineError(
            "invalid exact-duplicate baseline: name_collision_groups must be a list"
        )
    names: dict[str, set[str]] = {}
    for group in raw_names:
        if (
            not isinstance(group, dict)
            or set(group) != {"name", "paths"}
            or not isinstance(group["name"], str)
            or "::" not in group["name"]
            or not _sorted_unique_strings(group["paths"], 2)
        ):
            raise BaselineError("invalid exact-duplicate baseline: malformed name group")
        if group["name"] in names:
            raise BaselineError("invalid exact-duplicate baseline: repeated name group")
        names[group["name"]] = set(group["paths"])
    return bodies, names


def baseline_payload(
    observed: ScanResult,
    provisional: dict[str, str] | None,
) -> dict[str, object]:
    """Return the canonical baseline payload for observed groups."""
    payload: dict[str, object] = {
        "algorithm": ALGORITHM,
        "algorithm_description": ALGORITHM_DESCRIPTION,
        "body_duplicate_groups": sorted(sorted(ids) for ids in observed.body_groups.values()),
        "name_collision_groups": [
            {"name": name, "paths": sorted(paths)}
            for name, paths in sorted(observed.name_groups.items())
        ],
        "schema_version": SCHEMA_VERSION,
        "scope": SCOPE,
    }
    if provisional is not None:
        payload["provisional"] = provisional
    return payload


def main(argv: list[str] | None = None) -> int:
    """Check the baseline, or tighten it when only reductions are observed."""
    parser = argparse.ArgumentParser(
        description="Check per-test duplicate body and name no-growth ceilings."
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--update-baseline", action="store_true")
    parser.add_argument(
        "--allow-provisional",
        action="store_true",
        help="Allow explicitly provisional metadata during non-final checks.",
    )
    args = parser.parse_args(argv)

    root = args.root.resolve()
    baseline = args.baseline.resolve() if args.baseline is not None else root / DEFAULT_BASELINE
    try:
        payload = load_baseline(baseline, label="exact-duplicate")
        provisional = validate_provisional(
            payload,
            baseline,
            allow=args.allow_provisional,
            label="exact-duplicate",
        )
        allowed_bodies, allowed_names = parse_baseline(payload)
    except BaselineError as error:
        print(f"[x] {error}", file=sys.stderr)
        return 2

    try:
        modules = test_modules(root, SCOPE)
        observed = scan(root, modules)
    except (OSError, SyntaxError, UnicodeError, ValueError) as error:
        print(f"[x] exact-duplicate scan failed: {error}", file=sys.stderr)
        return 2
    body_growth, body_stale = compare_bodies(observed.body_groups, allowed_bodies)
    name_growth, name_stale = compare_names(observed.name_groups, allowed_names)
    growth = body_growth + name_growth
    stale = body_stale + name_stale
    if growth:
        for diagnostic in growth:
            print(f"[x] {diagnostic}", file=sys.stderr)
        if args.update_baseline:
            print(
                "[x] refusing to update exact-duplicate baseline with new debt; "
                "fix the listed copies instead",
                file=sys.stderr,
            )
        return 1
    if stale and not args.update_baseline:
        for diagnostic in stale:
            print(f"[x] {diagnostic}", file=sys.stderr)
        print(
            "[x] exact-duplicate baseline is stale after resolved copies. "
            f"Review the reductions, then run '{UPDATE_COMMAND}'.",
            file=sys.stderr,
        )
        return 1
    if stale:
        try:
            write_baseline(
                baseline,
                baseline_payload(observed, provisional),
                label="exact-duplicate",
            )
        except BaselineError as error:
            print(f"[x] {error}", file=sys.stderr)
            return 2
        print(
            "[+] updated exact-duplicate baseline: "
            f"{len(allowed_bodies)} -> {len(observed.body_groups)} body group(s), "
            f"{len(allowed_names)} -> {len(observed.name_groups)} name group(s)"
        )
    elif args.update_baseline:
        print("[i] exact-duplicate baseline already current; no update written")

    print(
        f"[+] exact test duplicate ratchet clean: {len(modules)} files, "
        f"{observed.test_count} tests, "
        f"{len(observed.body_groups)} allowed body duplicate group(s), "
        f"{len(observed.name_groups)} allowed name collision group(s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
