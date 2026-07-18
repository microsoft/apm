#!/usr/bin/env python3
"""Enforce canonical ownership for target-specific instruction contraction."""

from __future__ import annotations

import argparse
import ast
from pathlib import Path

_MANIFEST_OWNER = "reconcile_target_deployed_files"
_LIFECYCLE_ROUTER = "_reconcile_target_deployed_files"
_CLEANUP_OWNER = "reconcile_deployed_block"
_LOCAL_POST_PHASE = "run"


def _function_calls(source: str) -> dict[str, set[str]]:
    """Return direct call names indexed by enclosing function."""
    tree = ast.parse(source)
    calls: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        names: set[str] = set()
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            if isinstance(child.func, ast.Name):
                names.add(child.func.id)
        calls[node.name] = names
    return calls


def analyze_sources(
    manifest_source: str,
    lockfile_source: str,
    post_local_source: str,
) -> list[str]:
    """Return violations of target-file contraction ownership."""
    manifest_calls = _function_calls(manifest_source)
    lockfile_calls = _function_calls(lockfile_source)
    post_local_calls = _function_calls(post_local_source)
    violations: list[str] = []

    if _MANIFEST_OWNER not in manifest_calls:
        violations.append("target-file contraction owner is missing from manifest_reconcile.py")
    if _MANIFEST_OWNER not in manifest_calls.get("reconcile_deployed_state", set()):
        violations.append(
            "reconcile_deployed_state must delegate target files to manifest_reconcile"
        )
    if _CLEANUP_OWNER not in manifest_calls.get(_MANIFEST_OWNER, set()):
        violations.append(
            "target-file contraction owner must delegate deletion through reconcile_deployed_block"
        )
    if "remove_stale_deployed_files" not in manifest_calls.get(_CLEANUP_OWNER, set()):
        violations.append("target-file deletion must stay routed through reconcile_deployed_block")
    if "remove_stale_deployed_files" in lockfile_calls.get(_LIFECYCLE_ROUTER, set()):
        violations.append("LockfileBuilder must not delete target files directly")
    if _MANIFEST_OWNER not in lockfile_calls.get(_LIFECYCLE_ROUTER, set()):
        violations.append(
            "LockfileBuilder must route target contraction through manifest_reconcile"
        )
    if "remove_stale_deployed_files" in post_local_calls.get(_LOCAL_POST_PHASE, set()):
        violations.append("post-deps local must not delete target files directly")
    if _CLEANUP_OWNER not in post_local_calls.get(_LOCAL_POST_PHASE, set()):
        violations.append(
            "post-deps local must route target contraction through reconcile_deployed_block"
        )
    return violations


def analyze_paths(root: Path) -> list[str]:
    """Analyze the repository's manifest and install lifecycle consumers."""
    manifest_path = root / "src/apm_cli/install/manifest_reconcile.py"
    lockfile_path = root / "src/apm_cli/install/phases/lockfile.py"
    post_local_path = root / "src/apm_cli/install/phases/post_deps_local.py"
    return analyze_sources(
        manifest_path.read_text(encoding="utf-8"),
        lockfile_path.read_text(encoding="utf-8"),
        post_local_path.read_text(encoding="utf-8"),
    )


def main() -> int:
    """Print ownership violations and return a process status."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args()
    violations = analyze_paths(args.root)
    for violation in violations:
        print(violation)
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
