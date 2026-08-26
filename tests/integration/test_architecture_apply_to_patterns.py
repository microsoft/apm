"""Architecture guardrails for applyTo normalization and placement."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.component


def test_apply_to_normalization_and_hidden_placement_have_canonical_owners() -> None:
    """Parser and placement must route through their declared shared owners."""
    root = Path(__file__).parents[2]
    patterns = (root / "src/apm_cli/utils/patterns.py").read_text(encoding="utf-8")
    parser = (root / "src/apm_cli/primitives/parser.py").read_text(encoding="utf-8")
    optimizer = (root / "src/apm_cli/compilation/context_optimizer.py").read_text(encoding="utf-8")
    owner_table = (root / ".github/instructions/architecture.instructions.md").read_text(
        encoding="utf-8"
    )
    guard = (root / "scripts/lint-architecture-boundaries.sh").read_text(encoding="utf-8")

    assert patterns.count("def normalize_apply_to(") == 1
    assert patterns.count("def literal_apply_to_top_level_roots(") == 1
    assert "from apm_cli.utils.patterns import normalize_apply_to" in parser
    assert "def _normalize_apply_to(" not in parser
    assert "PLACEMENT_HIDDEN_TOOL_TREES = frozenset(" in optimizer
    assert "def _targeted_hidden_tool_roots(" in optimizer
    assert "literal_apply_to_top_level_roots(" in optimizer
    assert "def _targeted_top_level_roots(" not in optimizer
    assert "self._placement_hidden_tool_trees" in optimizer
    assert "not self._is_supported_hidden_tool_root(path)" in optimizer
    inventory = (root / "src/apm_cli/compilation/inventory.py").read_text(encoding="utf-8")
    inventory_guard = (root / "scripts/check_compile_inventory_authority.py").read_text(
        encoding="utf-8"
    )
    assert inventory.count("class CompileInventory") == 1
    assert inventory.count("os.walk(") == 1
    assert "os.walk(" not in optimizer
    assert "Compile traversal must route through compilation/inventory.py" in inventory_guard
    assert "| applyTo normalization and hidden-tool placement |" in owner_table
    assert (
        "applyTo parsing must use utils/patterns.py and hidden placement ContextOptimizer" in guard
    )


def test_apply_to_owner_guard_rejects_a_parser_normalizer(tmp_path: Path) -> None:
    """AC31 rejects restoring parser-local applyTo normalization."""
    root = Path(__file__).parents[2]
    sandbox = tmp_path / "repo"
    shutil.copytree(
        root,
        sandbox,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            ".pytest_cache",
            "__pycache__",
            "build",
            "dist",
            "node_modules",
        ),
    )
    parser = sandbox / "src/apm_cli/primitives/parser.py"
    parser.write_text(
        parser.read_text(encoding="utf-8")
        + '\n\ndef normalize_apply_to(value: object, default: str = "") -> str:\n'
        + "    return default\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        ("bash", "scripts/lint-architecture-boundaries.sh"),
        cwd=sandbox,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )

    assert result.returncode == 1
    assert "applyTo parsing must use utils/patterns.py and hidden placement ContextOptimizer" in (
        result.stdout
    )


def test_apply_to_owner_guard_rejects_optimizer_local_prefix_parser(tmp_path: Path) -> None:
    """AC31 rejects restoring local traversal-prefix parsing."""
    root = Path(__file__).parents[2]
    sandbox = tmp_path / "repo"
    shutil.copytree(
        root,
        sandbox,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            ".pytest_cache",
            "__pycache__",
            "build",
            "dist",
            "node_modules",
        ),
    )
    optimizer = sandbox / "src/apm_cli/compilation/context_optimizer.py"
    optimizer.write_text(
        optimizer.read_text(encoding="utf-8")
        + "\n    def _targeted_top_level_roots(self) -> frozenset[str]:\n"
        + "        return frozenset()\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        ("bash", "scripts/lint-architecture-boundaries.sh"),
        cwd=sandbox,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )

    assert result.returncode == 1
    assert (
        "applyTo parsing must use utils/patterns.py and hidden placement ContextOptimizer"
        in result.stdout
    )


def test_compile_inventory_guard_rejects_optimizer_walk(tmp_path: Path) -> None:
    """The optimizer must not restore a private project traversal."""
    root = Path(__file__).parents[2]
    sandbox = tmp_path / "repo"
    shutil.copytree(
        root,
        sandbox,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            ".pytest_cache",
            "__pycache__",
            "build",
            "dist",
            "node_modules",
        ),
    )
    optimizer = sandbox / "src/apm_cli/compilation/context_optimizer.py"
    optimizer.write_text(
        optimizer.read_text(encoding="utf-8") + "\n# os.walk(self.base_dir)\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        ("python3", "scripts/check_compile_inventory_authority.py"),
        cwd=sandbox,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )

    assert result.returncode == 1
    assert "Compile traversal must route through compilation/inventory.py" in result.stdout
