"""Tests for deployment owner authority boundary analysis."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def _load_checker() -> ModuleType:
    root = Path(__file__).parents[3]
    path = root / "scripts/check_deployment_owner_boundaries.py"
    spec = importlib.util.spec_from_file_location(
        "check_deployment_owner_boundaries",
        path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_current_consumers_satisfy_boundary() -> None:
    checker = _load_checker()
    root = Path(__file__).parents[3]

    for relative in (
        "src/apm_cli/commands/prune.py",
        "src/apm_cli/commands/audit.py",
        "src/apm_cli/policy/ci_checks.py",
    ):
        assert checker.analyze_path(root / relative) == []


def test_renamed_parallel_owner_filter_is_rejected() -> None:
    checker = _load_checker()
    source = """
def scrub(row, dependencies):
    survivors = tuple(item for item in row.owners if item in dependencies)
    return survivors
"""

    violations = checker.analyze_source(source, filename="consumer.py")

    assert any("owner filtering" in violation for violation in violations)


def test_renamed_manual_owner_filter_is_rejected() -> None:
    checker = _load_checker()
    source = """
def scrub(row, dependencies):
    survivors = []
    for candidate in row.owners:
        if candidate in dependencies:
            survivors.append(candidate)
    return survivors
"""

    violations = checker.analyze_source(source, filename="consumer.py")

    assert any("owner filtering" in violation for violation in violations)


def test_ghost_ledger_rows_cannot_authorize_cleanup() -> None:
    checker = _load_checker()
    source = """
def cleanup(owner_violations, root, diagnostics):
    remove_stale_deployed_files(
        {item.locator.value for item in owner_violations},
        root,
        dep_key="ghost",
        targets=None,
        diagnostics=diagnostics,
    )
"""

    violations = checker.analyze_source(source, filename="consumer.py")

    assert any("dependency claims" in violation for violation in violations)
    assert any("must not authorize" in violation for violation in violations)


def test_mutating_prune_codec_delegation_breaks_guard() -> None:
    checker = _load_checker()
    root = Path(__file__).parents[3]
    source = (root / "src/apm_cli/commands/prune.py").read_text(encoding="utf-8")
    mutated = source.replace(
        "DeploymentLedgerCodec.reconcile_owner_references(",
        "DeploymentLedgerCodec.skip_owner_references(",
        1,
    )

    violations = checker.analyze_source(mutated, filename="prune.py")

    assert any("reconcile_owner_references is missing" in item for item in violations)


def test_mutating_prune_legacy_projection_delegation_breaks_guard() -> None:
    checker = _load_checker()
    root = Path(__file__).parents[3]
    source = (root / "src/apm_cli/commands/prune.py").read_text(encoding="utf-8")
    mutated = source.replace(
        "DeploymentLedgerCodec.legacy_value(record.locator)",
        "record.locator.value",
        1,
    )

    violations = checker.analyze_source(mutated, filename="prune.py")

    assert any("legacy_value is missing" in item for item in violations)


def test_mutating_cleanup_to_ghost_selector_breaks_guard() -> None:
    checker = _load_checker()
    root = Path(__file__).parents[3]
    source = (root / "src/apm_cli/commands/prune.py").read_text(encoding="utf-8")
    mutated = source.replace(
        "trusted_paths - retained_paths,",
        "{item.locator.value for item in owner_violations},",
        1,
    )

    violations = checker.analyze_source(mutated, filename="prune.py")

    assert any("dependency claims" in item for item in violations)
    assert any("must not authorize" in item for item in violations)


def test_trusted_claim_union_with_renamed_ledger_rows_breaks_guard() -> None:
    checker = _load_checker()
    source = """
def cleanup(lockfile, stale, root, diagnostics):
    trusted_paths = set(lockfile.dependencies["beta"].deployed_files)
    remove_stale_deployed_files(
        trusted_paths | {item.locator.value for item in stale},
        root,
        dep_key="beta",
        targets=None,
        diagnostics=diagnostics,
    )
"""

    violations = checker.analyze_source(source, filename="consumer.py")

    assert any("must not authorize" in item for item in violations)
