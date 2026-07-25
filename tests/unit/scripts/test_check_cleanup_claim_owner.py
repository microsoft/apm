"""Mutation tests for the cleanup current-claim authority checker."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def _load_checker() -> ModuleType:
    root = Path(__file__).parents[3]
    path = root / "scripts/check_cleanup_claim_owner.py"
    spec = importlib.util.spec_from_file_location("check_cleanup_claim_owner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _valid_source() -> str:
    return """
from apm_cli.core.deployment_state import DeploymentReconciler

first = DeploymentReconciler.current_claimed_paths(package_deployed_files)
second = DeploymentReconciler.current_claimed_paths(package_deployed_files)
for key, paths in package_deployed_files.items():
    consume(key, paths, first, second)
"""


def test_checker_accepts_both_canonical_delegations() -> None:
    checker = _load_checker()

    assert checker.analyze_source(_valid_source()) == []


def test_checker_rejects_missing_either_delegation() -> None:
    checker = _load_checker()
    source = _valid_source().replace(
        "second = DeploymentReconciler.current_claimed_paths(package_deployed_files)",
        "second = frozenset()",
    )

    violations = checker.analyze_source(source)

    assert any("delegate both current-claim decisions" in item for item in violations)


def test_checker_rejects_renamed_local_union_loop() -> None:
    checker = _load_checker()
    source = (
        _valid_source()
        + """
claimed = set()
for bundle_paths in package_deployed_files.values():
    claimed.update(bundle_paths)
"""
    )

    violations = checker.analyze_source(source)

    assert any("local loop" in item for item in violations)


def test_checker_rejects_local_comprehension_union() -> None:
    checker = _load_checker()
    source = (
        _valid_source()
        + """
claimed = {
    deployed_path
    for bundle_paths in package_deployed_files.values()
    for deployed_path in bundle_paths
}
"""
    )

    violations = checker.analyze_source(source)

    assert any("local comprehension" in item for item in violations)
