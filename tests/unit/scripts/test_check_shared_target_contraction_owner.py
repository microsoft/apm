"""Mutation coverage for the shared target contraction owner guard."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


def _load_checker() -> ModuleType:
    """Load the semantic owner checker without adding scripts to sys.path."""
    root = Path(__file__).parents[3]
    path = root / "scripts" / "check_shared_target_contraction_owner.py"
    spec = importlib.util.spec_from_file_location("check_shared_target_contraction_owner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_checker_accepts_the_manifest_reconciliation_consumer() -> None:
    """The real consumer must delegate generic-row decisions to the reconciler."""
    root = Path(__file__).parents[3]
    checker = _load_checker()

    assert checker.analyze_path(root / "src/apm_cli/install/manifest_reconcile.py") == []


def test_checker_rejects_renamed_local_generic_row_algorithm() -> None:
    """AST detection must not rely on a retired helper name or lexical token."""
    checker = _load_checker()
    source = (
        "def reconcile_payload(ledger, targets):\n"
        "    DeploymentReconciler(None, targets, diagnostics=None).reconcile(ledger, (), None)\n"
        "    shadow_rows = {\n"
        "        entry.locator.value\n"
        "        for entry in ledger.records.values()\n"
        "        if entry.locator.target not in targets\n"
        "    }\n"
        "    return shadow_rows\n"
    )

    violations = checker.analyze_source(source)

    assert violations == [
        "line 1: generic deployment row supersession belongs to DeploymentReconciler"
    ]
