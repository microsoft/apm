#!/usr/bin/env python3
"""Prevent growth in proven raw-byte-identical Python test modules."""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from collections import defaultdict
from pathlib import Path

from ratchet_baseline import (
    BaselineError,
    load_baseline,
    validate_provisional,
    write_baseline,
)
from test_file_inventory import tracked_python_paths

ALGORITHM = "sha256-bytes-v1"
SCOPE = ["tests/**/test_*.py", "tests/**/*_test.py"]


def test_modules(root: Path, scope: list[str]) -> list[Path]:
    """Return contained, non-symlink tracked test modules for the scope."""
    return tracked_python_paths(root, scope=tuple(scope))


def duplicate_groups(root: Path, modules: list[Path]) -> dict[str, set[str]]:
    """Group scoped test modules by SHA-256 of their unmodified bytes."""
    by_hash: dict[str, set[str]] = defaultdict(set)
    for path in modules:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        by_hash[digest].add(path.relative_to(root).as_posix())
    return {digest: paths for digest, paths in by_hash.items() if len(paths) > 1}


def compare(
    observed: dict[str, set[str]],
    allowed: dict[str, set[str]],
) -> tuple[list[str], list[str]]:
    """Return new-debt and reduction diagnostics for exact groups."""
    growth: list[str] = []
    stale: list[str] = []
    for digest, paths in sorted(observed.items()):
        ceiling = allowed.get(digest)
        if ceiling is None:
            listed = "\n".join(f"  - {path}" for path in sorted(paths))
            growth.append(
                f"new exact duplicate group {digest}:\n{listed}\n"
                "  Remediation: make each test module meaningfully distinct; "
                "do not expand the baseline."
            )
        elif not paths <= ceiling:
            added = sorted(paths - ceiling)
            listed = "\n".join(f"  - {path}" for path in added)
            growth.append(
                f"exact duplicate group {digest} added tracked path(s):\n"
                f"{listed}\n"
                "  Remediation: remove or differentiate the added copy; "
                "do not expand the baseline."
            )
    for digest, paths in sorted(allowed.items()):
        current = observed.get(digest, set())
        if current != paths and current <= paths:
            stale.append(
                f"exact duplicate group {digest} reduced: {sorted(paths)} -> {sorted(current)}"
            )
    return growth, stale


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0911
    """Check the baseline, or tighten it when only reductions are observed."""
    parser = argparse.ArgumentParser(
        description="Check exact raw-byte test duplicate no-growth ceilings."
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
    baseline = (
        args.baseline.resolve()
        if args.baseline is not None
        else root / "tests" / "quality" / "exact_test_duplicates.json"
    )
    try:
        payload = load_baseline(baseline, label="exact-duplicate")
        provisional = validate_provisional(
            payload,
            baseline,
            allow=args.allow_provisional,
            label="exact-duplicate",
        )
    except BaselineError as error:
        print(f"[x] {error}", file=sys.stderr)
        return 2
    required_keys = {"algorithm", "duplicate_groups", "schema_version", "scope"}
    allowed_keys = required_keys | {"provisional"}
    if not required_keys <= set(payload) or not set(payload) <= allowed_keys:
        print(
            "[x] invalid exact-duplicate baseline: unexpected object shape",
            file=sys.stderr,
        )
        return 2
    if payload["schema_version"] != 1 or payload["algorithm"] != ALGORITHM:
        print(
            "[x] invalid exact-duplicate baseline: unsupported schema or algorithm",
            file=sys.stderr,
        )
        return 2
    if payload["scope"] != SCOPE:
        print(
            "[x] invalid exact-duplicate baseline: scope must remain exact",
            file=sys.stderr,
        )
        return 2

    raw_groups = payload["duplicate_groups"]
    if not isinstance(raw_groups, list):
        print(
            "[x] invalid exact-duplicate baseline: duplicate_groups must be a list",
            file=sys.stderr,
        )
        return 2
    allowed: dict[str, set[str]] = {}
    seen_paths: set[str] = set()
    for group in raw_groups:
        if not isinstance(group, dict) or set(group) != {"paths", "sha256"}:
            print(
                "[x] invalid exact-duplicate baseline: malformed group",
                file=sys.stderr,
            )
            return 2
        digest = group["sha256"]
        paths = group["paths"]
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            print(
                "[x] invalid exact-duplicate baseline: malformed sha256",
                file=sys.stderr,
            )
            return 2
        if (
            not isinstance(paths, list)
            or len(paths) < 2
            or any(not isinstance(path, str) or not path for path in paths)
            or paths != sorted(set(paths))
        ):
            print(
                "[x] invalid exact-duplicate baseline: paths must be sorted and unique",
                file=sys.stderr,
            )
            return 2
        if digest in allowed or seen_paths.intersection(paths):
            print(
                "[x] invalid exact-duplicate baseline: repeated digest or path",
                file=sys.stderr,
            )
            return 2
        allowed[digest] = set(paths)
        seen_paths.update(paths)

    try:
        modules = test_modules(root, payload["scope"])
        observed = duplicate_groups(root, modules)
    except (OSError, UnicodeError, ValueError) as error:
        print(f"[x] exact-duplicate scan failed: {error}", file=sys.stderr)
        return 2
    growth, stale = compare(observed, allowed)
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
        command = "uv run --frozen python scripts/check_exact_test_duplicates.py --update-baseline"
        print(
            "[x] exact-duplicate baseline is stale after resolved copies. "
            f"Review the reductions, then run '{command}'.",
            file=sys.stderr,
        )
        return 1
    if stale:
        removed_groups = len(set(allowed) - set(observed))
        removed_paths = sum(len(paths) for paths in allowed.values()) - sum(
            len(paths) for paths in observed.values()
        )
        updated_groups = [
            {"paths": sorted(paths), "sha256": digest} for digest, paths in sorted(observed.items())
        ]
        updated: dict[str, object] = {
            "algorithm": ALGORITHM,
            "duplicate_groups": updated_groups,
            "schema_version": 1,
            "scope": SCOPE,
        }
        if provisional is not None:
            updated["provisional"] = provisional
        try:
            write_baseline(
                baseline,
                updated,
                label="exact-duplicate",
            )
        except BaselineError as error:
            print(f"[x] {error}", file=sys.stderr)
            return 2
        print(
            "[+] updated exact-duplicate baseline: removed "
            f"{removed_groups} resolved group(s) and "
            f"{removed_paths} stale path entr{'y' if removed_paths == 1 else 'ies'}"
        )
    elif args.update_baseline:
        print("[i] exact-duplicate baseline already current; no update written")

    print(
        f"[+] exact test duplicate ratchet clean: {len(modules)} files, "
        f"{len(observed)} allowed duplicate group(s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
