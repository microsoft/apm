"""Architecture guards for canonical frontmatter BOM decoding."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.component


def test_frontmatter_bom_decoding_has_single_owner() -> None:
    """The shared frontmatter loader must own path and stream BOM handling."""
    root = Path(__file__).parents[2]
    owner = (root / "src/apm_cli/utils/yaml_io.py").read_text(encoding="utf-8")
    guard = (root / "scripts/lint-architecture-boundaries.sh").read_text(encoding="utf-8")
    architecture_doc = (root / ".apm/instructions/architecture.instructions.md").read_text(
        encoding="utf-8"
    )

    assert 'def load_frontmatter(fd: Any, encoding: str = "utf-8-sig")' in owner
    assert 'text.removeprefix("\\ufeff")' in owner
    assert "AC36: frontmatter BOM decoding authority" in guard
    assert "Frontmatter BOM decoding must route through utils/yaml_io.py" in guard
    assert "| Frontmatter BOM decoding and bounded YAML parsing |" in architecture_doc

    duplicate_owners = []
    for source in (root / "src/apm_cli").rglob("*.py"):
        if source == root / "src/apm_cli/utils/yaml_io.py":
            continue
        if "utf-8-sig" in source.read_text(encoding="utf-8"):
            duplicate_owners.append(source.relative_to(root).as_posix())
    assert duplicate_owners == []


def test_frontmatter_bom_guard_rejects_caller_owned_encoding(tmp_path: Path) -> None:
    """The boundary lint rejects BOM decoding duplicated in a consumer."""
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
    consumer = sandbox / "src/apm_cli/primitives/parser.py"
    consumer.write_text(
        consumer.read_text(encoding="utf-8").replace(
            'open(file_path, encoding="utf-8")',
            'open(file_path, encoding="utf-8-sig")',
            1,
        ),
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
    assert "Frontmatter BOM decoding must route through utils/yaml_io.py" in result.stdout
