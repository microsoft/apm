"""Architectural invariants for the install engine package.

These tests are the structural defence against regression to a
god-function/god-module design. They are intentionally activated as the
modularization refactor progresses; LOC budgets are set to current actuals
and tightened as more code is extracted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ENGINE_ROOT = Path(__file__).resolve().parents[3] / "src" / "apm_cli" / "install"


def _line_count(path: Path) -> int:
    return sum(1 for _ in path.read_text(encoding="utf-8").splitlines())


def test_engine_package_exists():
    """The engine package must exist as a sibling of commands/."""
    assert ENGINE_ROOT.is_dir(), f"{ENGINE_ROOT} is missing"
    assert (ENGINE_ROOT / "__init__.py").is_file()
    assert (ENGINE_ROOT / "context.py").is_file()
    assert (ENGINE_ROOT / "phases").is_dir()
    assert (ENGINE_ROOT / "helpers").is_dir()
    assert (ENGINE_ROOT / "presentation").is_dir()


def test_install_context_importable():
    """InstallContext is the contract carrying state between phases."""
    from apm_cli.install.context import InstallContext

    assert hasattr(InstallContext, "__dataclass_fields__"), (
        "InstallContext must be a dataclass"
    )


@pytest.mark.skip(reason="LOC budget activated in P3.R2 once extraction is complete")
def test_no_install_module_exceeds_500_loc():
    """No file in the engine package should grow past 500 LOC.

    This is the structural guard against the install.py mega-function ever
    growing back. Activated in P3.R2 of the refactor with the final budget.
    """
    offenders = []
    for path in ENGINE_ROOT.rglob("*.py"):
        n = _line_count(path)
        if n > 500:
            offenders.append((path.relative_to(ENGINE_ROOT), n))
    assert not offenders, f"Modules exceeding 500 LOC: {offenders}"
