"""Mutation coverage for the target-specific instruction contraction guard."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


def _load_checker() -> ModuleType:
    """Load the checker without adding scripts to sys.path."""
    root = Path(__file__).parents[3]
    path = root / "scripts/check_target_instruction_contraction_owner.py"
    spec = importlib.util.spec_from_file_location(
        "check_target_instruction_contraction_owner", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_checker_accepts_the_canonical_target_file_contraction_route() -> None:
    """The product route must use manifest reconciliation and the cleanup chokepoint."""
    root = Path(__file__).parents[3]
    checker = _load_checker()

    assert checker.analyze_paths(root) == []


def test_checker_rejects_lockfile_direct_cleanup_mutation() -> None:
    """A lifecycle-local unlink route must fail the static ownership boundary."""
    checker = _load_checker()
    manifest_source = """
def reconcile_deployed_block():
    remove_stale_deployed_files()

def reconcile_target_deployed_files():
    reconcile_deployed_block()

def reconcile_deployed_state():
    reconcile_target_deployed_files()
"""
    lockfile_source = """
def _reconcile_target_deployed_files():
    remove_stale_deployed_files()
"""

    assert checker.analyze_sources(manifest_source, lockfile_source) == [
        "LockfileBuilder must not delete target files directly",
        "LockfileBuilder must route target contraction through manifest_reconcile",
    ]


def test_checker_rejects_manifest_owner_without_cleanup_delegation() -> None:
    """A no-op manifest owner must not satisfy the contraction boundary."""
    checker = _load_checker()
    manifest_source = """
def reconcile_deployed_block():
    remove_stale_deployed_files()

def reconcile_target_deployed_files():
    return False

def reconcile_deployed_state():
    reconcile_target_deployed_files()
"""
    lockfile_source = """
def _reconcile_target_deployed_files():
    reconcile_target_deployed_files()
"""

    assert checker.analyze_sources(manifest_source, lockfile_source) == [
        "target-file contraction owner must delegate deletion through reconcile_deployed_block"
    ]
